/**
 * __tests__/LoadingSpinner.test.tsx
 * Tests for frontend/src/components/LoadingSpinner.tsx
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { LoadingSpinner } from '../components/LoadingSpinner';

describe('LoadingSpinner', () => {
  it('renders with role="status"', () => {
    render(<LoadingSpinner status="uploading" />);
    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('renders "Uploading" heading for status="uploading"', () => {
    render(<LoadingSpinner status="uploading" />);
    expect(screen.getByText(/uploading/i)).toBeInTheDocument();
  });

  it('renders "Analyzing" heading for status="analyzing"', () => {
    render(<LoadingSpinner status="analyzing" />);
    expect(screen.getByText(/analyzing/i)).toBeInTheDocument();
  });

  it('renders upload subtext for status="uploading"', () => {
    render(<LoadingSpinner status="uploading" />);
    expect(screen.getByText(/securely/i)).toBeInTheDocument();
  });

  it('renders analysis subtext for status="analyzing"', () => {
    render(<LoadingSpinner status="analyzing" />);
    expect(screen.getByText(/Medicare reference rates/i)).toBeInTheDocument();
  });

  it('renders a spinner element', () => {
    render(<LoadingSpinner status="uploading" />);
    // The spinner has data-testid="loading-spinner"
    expect(screen.getByTestId('loading-spinner')).toBeInTheDocument();
  });

  it('does not show "analyzing" text when status is "uploading"', () => {
    render(<LoadingSpinner status="uploading" />);
    // Should not render the analyzing-specific subtext
    expect(screen.queryByText(/AI is reviewing/i)).not.toBeInTheDocument();
  });

  it('does not show "uploading" subtext when status is "analyzing"', () => {
    render(<LoadingSpinner status="analyzing" />);
    expect(screen.queryByText(/Sending your file/i)).not.toBeInTheDocument();
  });
});
