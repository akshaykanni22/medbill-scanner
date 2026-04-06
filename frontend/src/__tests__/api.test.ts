/**
 * __tests__/api.test.ts
 * Tests for frontend/src/utils/api.ts
 * Mocks the global fetch using jest.spyOn.
 */

import { analyzeBill, checkHealth, ApiError } from '../utils/api';
import type { AnalysisResponse, HealthResponse } from '../types';

// ---- Helpers ----

function mockFetchOk(body: unknown): jest.SpyInstance {
  return jest.spyOn(global, 'fetch').mockResolvedValueOnce(
    new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }),
  );
}

function mockFetchError(status: number, body: unknown): jest.SpyInstance {
  return jest.spyOn(global, 'fetch').mockResolvedValueOnce(
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
  );
}

function mockFetchNetworkError(): jest.SpyInstance {
  return jest.spyOn(global, 'fetch').mockRejectedValueOnce(new Error('Network error'));
}

function makeMockFile(name = 'bill.pdf', type = 'application/pdf'): File {
  return new File(['fake content'], name, { type });
}

const MOCK_ANALYSIS_RESPONSE: AnalysisResponse = {
  anomalies: [],
  dispute_letter: null,
  bill_summary: {
    total_line_items: 3,
    total_billed_amount: null,
    anomaly_count: 0,
    high_severity_count: 0,
    medium_severity_count: 0,
    potential_overcharge_total: null,
  },
  processing_time_seconds: 2.5,
};

const MOCK_HEALTH_RESPONSE: HealthResponse = {
  status: 'ok',
  chromadb_connected: true,
  collection_size: 1000,
};

// ---- analyzeBill() ----

describe('analyzeBill', () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('returns AnalysisResponse on successful POST', async () => {
    mockFetchOk(MOCK_ANALYSIS_RESPONSE);
    const file = makeMockFile();
    const result = await analyzeBill(file);
    expect(result).toEqual(MOCK_ANALYSIS_RESPONSE);
  });

  it('calls the correct endpoint', async () => {
    const spy = mockFetchOk(MOCK_ANALYSIS_RESPONSE);
    const file = makeMockFile();
    await analyzeBill(file);
    expect(spy).toHaveBeenCalledWith(
      expect.stringContaining('/api/analyze'),
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('sends the file as FormData', async () => {
    const spy = mockFetchOk(MOCK_ANALYSIS_RESPONSE);
    const file = makeMockFile();
    await analyzeBill(file);
    const [, options] = spy.mock.calls[0];
    expect(options.body).toBeInstanceOf(FormData);
  });

  it('throws ApiError on 413 response', async () => {
    mockFetchError(413, { error: 'file_too_large', detail: 'File exceeds the limit.' });
    const file = makeMockFile();
    await expect(analyzeBill(file)).rejects.toThrow(ApiError);
  });

  it('thrown ApiError has correct code and status', async () => {
    mockFetchError(415, { error: 'invalid_file_type', detail: 'Not supported.' });
    const file = makeMockFile();
    try {
      await analyzeBill(file);
      fail('should have thrown');
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).code).toBe('invalid_file_type');
      expect((err as ApiError).status).toBe(415);
    }
  });

  it('throws ApiError on 422 response', async () => {
    mockFetchError(422, { error: 'ocr_failed', detail: 'Could not extract text.' });
    await expect(analyzeBill(makeMockFile())).rejects.toThrow(ApiError);
  });

  it('throws ApiError on 500 response', async () => {
    mockFetchError(500, { error: 'analysis_failed', detail: 'Internal error.' });
    await expect(analyzeBill(makeMockFile())).rejects.toThrow(ApiError);
  });

  it('throws generic Error on network failure', async () => {
    mockFetchNetworkError();
    await expect(analyzeBill(makeMockFile())).rejects.toThrow('Network error');
  });

  it('passes AbortSignal to fetch', async () => {
    const spy = mockFetchOk(MOCK_ANALYSIS_RESPONSE);
    const file = makeMockFile();
    const controller = new AbortController();
    await analyzeBill(file, controller.signal);
    const [, options] = spy.mock.calls[0];
    expect(options.signal).toBe(controller.signal);
  });

  it('throws AbortError when aborted', async () => {
    const abortError = new DOMException('Aborted', 'AbortError');
    jest.spyOn(global, 'fetch').mockRejectedValueOnce(abortError);
    const controller = new AbortController();
    await expect(analyzeBill(makeMockFile(), controller.signal)).rejects.toMatchObject({
      name: 'AbortError',
    });
  });

  it('handles non-JSON error response gracefully', async () => {
    // Proxy or nginx HTML error page
    jest.spyOn(global, 'fetch').mockResolvedValueOnce(
      new Response('<html>502 Bad Gateway</html>', {
        status: 502,
        headers: { 'Content-Type': 'text/html' },
      }),
    );
    const err = await analyzeBill(makeMockFile()).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(502);
    expect((err as ApiError).code).toBe('unknown_error');
  });
});

// ---- checkHealth() ----

describe('checkHealth', () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('returns HealthResponse on 200', async () => {
    mockFetchOk(MOCK_HEALTH_RESPONSE);
    const result = await checkHealth();
    expect(result).toEqual(MOCK_HEALTH_RESPONSE);
  });

  it('calls the health endpoint', async () => {
    const spy = mockFetchOk(MOCK_HEALTH_RESPONSE);
    await checkHealth();
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('/api/health'));
  });

  it('throws ApiError on HTTP error', async () => {
    mockFetchError(500, { error: 'server_error', detail: 'Internal error.' });
    await expect(checkHealth()).rejects.toThrow(ApiError);
  });
});

// ---- ApiError class ----

describe('ApiError', () => {
  it('has name === ApiError', () => {
    const err = new ApiError(429, { error: 'rate_limit_exceeded', detail: 'Too many requests.' });
    expect(err.name).toBe('ApiError');
  });

  it('stores code, detail, and status', () => {
    const err = new ApiError(413, { error: 'file_too_large', detail: 'Too big.' });
    expect(err.code).toBe('file_too_large');
    expect(err.detail).toBe('Too big.');
    expect(err.status).toBe(413);
  });

  it('instanceof Error is true', () => {
    const err = new ApiError(400, { error: 'bad_request', detail: 'Bad.' });
    expect(err).toBeInstanceOf(Error);
  });

  it('message equals detail', () => {
    const err = new ApiError(400, { error: 'x', detail: 'Human message.' });
    expect(err.message).toBe('Human message.');
  });
});
