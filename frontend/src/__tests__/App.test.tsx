/**
 * __tests__/App.test.tsx
 * Tests for frontend/src/App.tsx — top-level orchestration
 */

import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import App from '../App';
import * as api from '../utils/api';
import type { AnalysisResponse, HealthResponse } from '../types';

// ---- Mock data ----

const MOCK_HEALTH_OK: HealthResponse = {
  status: 'ok',
  chromadb_connected: true,
  collection_size: 1000,
};

const MOCK_HEALTH_DEGRADED: HealthResponse = {
  status: 'degraded',
  chromadb_connected: true,
  collection_size: 0,
};

const MOCK_HEALTH_UNAVAILABLE: HealthResponse = {
  status: 'unavailable',
  chromadb_connected: false,
  collection_size: 0,
};

const MOCK_RESULT_CLEAN: AnalysisResponse = {
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
  processing_time_seconds: 1.5,
};

const MOCK_RESULT_WITH_ANOMALY: AnalysisResponse = {
  anomalies: [{
    line_item: {
      code: '99213',
      description: 'Office visit',
      quantity: 1,
      billed_amount: 300.0,
      service_date: null,
    },
    anomaly_type: 'price_overcharge',
    severity: 'high',
    explanation: 'Overpriced.',
    suggested_action: 'Dispute it.',
    medicare_reference_price: 100.0,
    overcharge_ratio: 3.0,
  }],
  dispute_letter: {
    subject_line: 'Dispute: Possible Overcharge',
    body: 'Dear Billing Department,\n\nCode 99213 was overcharged.',
    anomaly_codes: ['99213'],
  },
  bill_summary: {
    total_line_items: 5,
    total_billed_amount: null,
    anomaly_count: 1,
    high_severity_count: 1,
    medium_severity_count: 0,
    potential_overcharge_total: 200.0,
  },
  processing_time_seconds: 2.5,
};

// ---- Tests ----

describe('App', () => {
  beforeEach(() => {
    // Default: health check passes
    jest.spyOn(api, 'checkHealth').mockResolvedValue(MOCK_HEALTH_OK);
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.runOnlyPendingTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  // ---- Idle state ----

  it('renders BillUploader in idle state', async () => {
    render(<App />);
    await act(async () => { await Promise.resolve(); }); // let health check run
    expect(screen.getByTestId('bill-uploader')).toBeInTheDocument();
  });

  it('renders the app header', async () => {
    render(<App />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText('MedBill Scanner')).toBeInTheDocument();
  });

  it('does not show health warning when health is ok', async () => {
    jest.spyOn(api, 'checkHealth').mockResolvedValue(MOCK_HEALTH_OK);
    render(<App />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId('health-warning')).not.toBeInTheDocument();
  });

  // ---- Health warning banner ----

  it('shows health warning when health is degraded', async () => {
    jest.spyOn(api, 'checkHealth').mockResolvedValue(MOCK_HEALTH_DEGRADED);
    render(<App />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId('health-warning')).toBeInTheDocument();
    expect(screen.getByTestId('health-warning').textContent).toMatch(/Setup needed/i);
  });

  it('shows health warning when health is unavailable', async () => {
    jest.spyOn(api, 'checkHealth').mockResolvedValue(MOCK_HEALTH_UNAVAILABLE);
    render(<App />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId('health-warning')).toBeInTheDocument();
    expect(screen.getByTestId('health-warning').textContent).toMatch(/unavailable/i);
  });

  it('shows health warning when health check throws', async () => {
    jest.spyOn(api, 'checkHealth').mockRejectedValue(new Error('Network'));
    render(<App />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId('health-warning')).toBeInTheDocument();
  });

  // ---- Loading spinner ----

  it('renders LoadingSpinner during uploading phase', async () => {
    jest.spyOn(api, 'analyzeBill').mockReturnValue(new Promise(() => {}));
    render(<App />);
    await act(async () => { await Promise.resolve(); });

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['fake'], 'bill.pdf', { type: 'application/pdf' });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });
    fireEvent.submit(screen.getByTestId('bill-uploader'));

    expect(screen.getByTestId('loading-spinner')).toBeInTheDocument();
  });

  // ---- Results ----

  it('renders AnomalyReport after successful analysis', async () => {
    jest.spyOn(api, 'analyzeBill').mockResolvedValue(MOCK_RESULT_CLEAN);
    render(<App />);
    await act(async () => { await Promise.resolve(); });

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['fake'], 'bill.pdf', { type: 'application/pdf' });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });
    await act(async () => {
      fireEvent.submit(screen.getByTestId('bill-uploader'));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.queryByTestId('anomaly-report') || screen.queryByTestId('clean-bill-message')).toBeTruthy();
    });
  });

  it('renders DisputeLetter when anomalies and letter are present', async () => {
    jest.spyOn(api, 'analyzeBill').mockResolvedValue(MOCK_RESULT_WITH_ANOMALY);
    render(<App />);
    await act(async () => { await Promise.resolve(); });

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['fake'], 'bill.pdf', { type: 'application/pdf' });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });
    await act(async () => {
      fireEvent.submit(screen.getByTestId('bill-uploader'));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.queryByTestId('dispute-letter')).toBeInTheDocument();
    });
  });

  // ---- Error state ----

  it('renders error card on API error', async () => {
    const apiErr = new api.ApiError(429, { error: 'rate_limit_exceeded', detail: 'Too many requests.' });
    jest.spyOn(api, 'analyzeBill').mockRejectedValue(apiErr);
    render(<App />);
    await act(async () => { await Promise.resolve(); });

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['fake'], 'bill.pdf', { type: 'application/pdf' });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });
    await act(async () => {
      fireEvent.submit(screen.getByTestId('bill-uploader'));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.getByTestId('error-card')).toBeInTheDocument();
    });
  });
});
