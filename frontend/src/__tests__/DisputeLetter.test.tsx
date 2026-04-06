/**
 * __tests__/DisputeLetter.test.tsx
 * Tests for frontend/src/components/DisputeLetter.tsx
 */

import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { DisputeLetter } from '../components/DisputeLetter';
import type { DisputeLetter as DisputeLetterType } from '../types';

// ---- Helpers ----

function makeLetter(overrides: Partial<DisputeLetterType> = {}): DisputeLetterType {
  return {
    subject_line: 'Dispute: Possible Overcharge — Itemized Review Requested',
    body: 'Dear Billing Department,\n\nI am writing to dispute charges for code 99213.',
    anomaly_codes: ['99213'],
    ...overrides,
  };
}

// Mock clipboard API
const writeText = jest.fn();
Object.defineProperty(navigator, 'clipboard', {
  value: { writeText },
  writable: true,
});

// ---- Tests ----

describe('DisputeLetter', () => {
  beforeEach(() => {
    writeText.mockClear();
    writeText.mockResolvedValue(undefined);
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.runOnlyPendingTimers();
    jest.useRealTimers();
  });

  it('renders the letter body', () => {
    render(<DisputeLetter letter={makeLetter()} onCodesHover={jest.fn()} />);
    expect(screen.getByTestId('letter-body')).toBeInTheDocument();
    expect(screen.getByText(/Dear Billing Department/)).toBeInTheDocument();
  });

  it('renders the subject line', () => {
    render(<DisputeLetter letter={makeLetter()} onCodesHover={jest.fn()} />);
    expect(screen.getByText('Dispute: Possible Overcharge — Itemized Review Requested')).toBeInTheDocument();
  });

  it('renders the copy button', () => {
    render(<DisputeLetter letter={makeLetter()} onCodesHover={jest.fn()} />);
    expect(screen.getByTestId('copy-button')).toBeInTheDocument();
  });

  it('copy button calls navigator.clipboard.writeText with letter body', async () => {
    const letter = makeLetter();
    render(<DisputeLetter letter={letter} onCodesHover={jest.fn()} />);
    await act(async () => {
      fireEvent.click(screen.getByTestId('copy-button'));
    });
    expect(writeText).toHaveBeenCalledWith(letter.body);
  });

  it('copy button shows "Copied!" after successful copy', async () => {
    render(<DisputeLetter letter={makeLetter()} onCodesHover={jest.fn()} />);
    await act(async () => {
      fireEvent.click(screen.getByTestId('copy-button'));
    });
    expect(screen.getByTestId('copy-button')).toHaveTextContent('Copied!');
  });

  it('copy button resets to "Copy letter" after 2 seconds', async () => {
    render(<DisputeLetter letter={makeLetter()} onCodesHover={jest.fn()} />);
    await act(async () => {
      fireEvent.click(screen.getByTestId('copy-button'));
    });
    expect(screen.getByTestId('copy-button')).toHaveTextContent('Copied!');

    act(() => {
      jest.advanceTimersByTime(2100);
    });

    expect(screen.getByTestId('copy-button')).toHaveTextContent('Copy letter');
  });

  it('HCPCS codes are highlighted in letter body', () => {
    const letter = makeLetter({
      body: 'Regarding code 99213 on your bill.',
      anomaly_codes: ['99213'],
    });
    render(<DisputeLetter letter={letter} onCodesHover={jest.fn()} />);
    // The code should appear as a <mark> element
    const marks = screen.getAllByText('99213');
    const markElement = marks.find((el) => el.tagName === 'MARK');
    expect(markElement).toBeTruthy();
  });

  it('calls onCodesHover with code Set when mark is hovered', () => {
    const onCodesHover = jest.fn();
    const letter = makeLetter({
      body: 'Code 99213 was overcharged.',
      anomaly_codes: ['99213'],
    });
    render(<DisputeLetter letter={letter} onCodesHover={onCodesHover} />);

    const marks = document.querySelectorAll('mark');
    expect(marks.length).toBeGreaterThan(0);
    fireEvent.mouseEnter(marks[0]);

    expect(onCodesHover).toHaveBeenCalledWith(expect.any(Set));
    const calledWith = onCodesHover.mock.calls[0][0] as Set<string>;
    expect(calledWith.has('99213')).toBe(true);
  });

  it('calls onCodesHover(null) on mouse leave', () => {
    const onCodesHover = jest.fn();
    const letter = makeLetter({
      body: 'Code 99213 was overcharged.',
      anomaly_codes: ['99213'],
    });
    render(<DisputeLetter letter={letter} onCodesHover={onCodesHover} />);

    const marks = document.querySelectorAll('mark');
    fireEvent.mouseEnter(marks[0]);
    fireEvent.mouseLeave(marks[0]);

    // Last call should be null
    const lastCall = onCodesHover.mock.calls[onCodesHover.mock.calls.length - 1];
    expect(lastCall[0]).toBeNull();
  });

  it('does not render highlight hint when anomaly_codes is empty', () => {
    const letter = makeLetter({ anomaly_codes: [] });
    render(<DisputeLetter letter={letter} onCodesHover={jest.fn()} />);
    // The "Hover over a highlighted code" hint should not be shown
    expect(screen.queryByText(/Hover over a/i)).not.toBeInTheDocument();
  });

  it('renders highlight hint when anomaly_codes is non-empty', () => {
    const letter = makeLetter({ anomaly_codes: ['99213'] });
    render(<DisputeLetter letter={letter} onCodesHover={jest.fn()} />);
    expect(screen.getByText(/Hover over a/i)).toBeInTheDocument();
  });

  it('shows "Copy failed" text when clipboard is unavailable', async () => {
    // Temporarily remove clipboard
    const original = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', { value: undefined, writable: true });

    render(<DisputeLetter letter={makeLetter()} onCodesHover={jest.fn()} />);
    await act(async () => {
      fireEvent.click(screen.getByTestId('copy-button'));
    });
    expect(screen.getByTestId('copy-button')).toHaveTextContent(/Copy failed/i);

    // Restore
    Object.defineProperty(navigator, 'clipboard', { value: original, writable: true });
  });
});
