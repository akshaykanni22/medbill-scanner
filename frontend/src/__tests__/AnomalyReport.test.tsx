/**
 * __tests__/AnomalyReport.test.tsx
 * Tests for frontend/src/components/AnomalyReport.tsx
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { AnomalyReport, SummaryBar, AnomalyCard } from '../components/AnomalyReport';
import type { Anomaly, BillSummary } from '../types';

// ---- Helpers ----

function makeAnomaly(overrides: Partial<Anomaly> = {}): Anomaly {
  return {
    line_item: {
      code: '99213',
      description: 'Office visit established patient',
      quantity: 1,
      billed_amount: 300.0,
      service_date: '05/01/2024',
    },
    anomaly_type: 'price_overcharge',
    severity: 'high',
    explanation: 'Billed 3x the Medicare reference rate.',
    medicare_reference_price: 100.0,
    overcharge_ratio: 3.0,
    suggested_action: 'Request an itemized bill.',
    ...overrides,
  };
}

function makeSummary(overrides: Partial<BillSummary> = {}): BillSummary {
  return {
    total_line_items: 10,
    total_billed_amount: null,
    anomaly_count: 1,
    high_severity_count: 1,
    medium_severity_count: 0,
    potential_overcharge_total: 200.0,
    ...overrides,
  };
}

// ---- SummaryBar ----

describe('SummaryBar', () => {
  it('renders total line items count', () => {
    render(<SummaryBar summary={makeSummary({ total_line_items: 7 })} />);
    expect(screen.getByText('7')).toBeInTheDocument();
  });

  it('renders anomaly count', () => {
    render(<SummaryBar summary={makeSummary({ anomaly_count: 3 })} />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('renders high severity count', () => {
    render(<SummaryBar summary={makeSummary({ high_severity_count: 2 })} />);
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('renders potential overcharge amount when available', () => {
    render(<SummaryBar summary={makeSummary({ potential_overcharge_total: 450.0 })} />);
    expect(screen.getByText(/\$450/)).toBeInTheDocument();
  });

  it('renders N/A when potential overcharge is null', () => {
    render(<SummaryBar summary={makeSummary({ potential_overcharge_total: null })} />);
    expect(screen.getByText('N/A')).toBeInTheDocument();
  });
});

// ---- AnomalyCard ----

describe('AnomalyCard', () => {
  it('renders the HCPCS code', () => {
    render(<AnomalyCard anomaly={makeAnomaly()} isHighlighted={false} index={0} />);
    expect(screen.getByText('99213')).toBeInTheDocument();
  });

  it('renders the service description', () => {
    render(<AnomalyCard anomaly={makeAnomaly()} isHighlighted={false} index={0} />);
    expect(screen.getByText('Office visit established patient')).toBeInTheDocument();
  });

  it('renders severity badge', () => {
    render(<AnomalyCard anomaly={makeAnomaly({ severity: 'high' })} isHighlighted={false} index={0} />);
    expect(screen.getByText('High')).toBeInTheDocument();
  });

  it('renders anomaly type label', () => {
    render(<AnomalyCard anomaly={makeAnomaly({ anomaly_type: 'duplicate_charge' })} isHighlighted={false} index={0} />);
    expect(screen.getByText('Duplicate Charge')).toBeInTheDocument();
  });

  it('applies ring class when isHighlighted=true', () => {
    const { container } = render(
      <AnomalyCard anomaly={makeAnomaly()} isHighlighted={true} index={0} />,
    );
    expect(container.firstChild).toHaveClass('ring-2');
  });

  it('does not apply ring class when isHighlighted=false', () => {
    const { container } = render(
      <AnomalyCard anomaly={makeAnomaly()} isHighlighted={false} index={0} />,
    );
    expect(container.firstChild).not.toHaveClass('ring-2');
  });

  it('detail section is hidden before click', () => {
    render(<AnomalyCard anomaly={makeAnomaly()} isHighlighted={false} index={0} />);
    expect(screen.queryByText('Billed 3x the Medicare reference rate.')).not.toBeInTheDocument();
  });

  it('expands detail section on header click', () => {
    render(<AnomalyCard anomaly={makeAnomaly()} isHighlighted={false} index={0} />);
    const headerButton = screen.getByRole('button', { expanded: false });
    fireEvent.click(headerButton);
    expect(screen.getByText('Billed 3x the Medicare reference rate.')).toBeInTheDocument();
  });

  it('shows suggested action when expanded', () => {
    render(<AnomalyCard anomaly={makeAnomaly()} isHighlighted={false} index={0} />);
    fireEvent.click(screen.getByRole('button', { expanded: false }));
    expect(screen.getByText('Request an itemized bill.')).toBeInTheDocument();
  });

  it('collapses on second click', () => {
    render(<AnomalyCard anomaly={makeAnomaly()} isHighlighted={false} index={0} />);
    const btn = screen.getByRole('button', { expanded: false });
    fireEvent.click(btn);
    fireEvent.click(screen.getByRole('button', { expanded: true }));
    expect(screen.queryByText('Billed 3x the Medicare reference rate.')).not.toBeInTheDocument();
  });

  it('shows billed amount', () => {
    render(<AnomalyCard anomaly={makeAnomaly({ line_item: { code: '99213', description: 'x', quantity: 1, billed_amount: 300.0, service_date: null } })} isHighlighted={false} index={0} />);
    expect(screen.getByText(/\$300/)).toBeInTheDocument();
  });

  it('shows "Amount N/A" when billed_amount is null', () => {
    const anomaly = makeAnomaly();
    anomaly.line_item = { ...anomaly.line_item, billed_amount: null };
    render(<AnomalyCard anomaly={anomaly} isHighlighted={false} index={0} />);
    expect(screen.getByText('Amount N/A')).toBeInTheDocument();
  });
});

// ---- AnomalyReport ----

describe('AnomalyReport', () => {
  it('renders clean bill message when anomalies is empty', () => {
    render(<AnomalyReport anomalies={[]} billSummary={makeSummary({ anomaly_count: 0 })} />);
    expect(screen.getByTestId('clean-bill-message')).toBeInTheDocument();
    expect(screen.getByText(/No issues found/i)).toBeInTheDocument();
  });

  it('does not render clean bill message when anomalies exist', () => {
    render(<AnomalyReport anomalies={[makeAnomaly()]} billSummary={makeSummary()} />);
    expect(screen.queryByTestId('clean-bill-message')).not.toBeInTheDocument();
  });

  it('renders an anomaly card for each anomaly', () => {
    const anomalies = [makeAnomaly(), makeAnomaly({ line_item: { ...makeAnomaly().line_item, code: '36415' } })];
    render(<AnomalyReport anomalies={anomalies} billSummary={makeSummary({ anomaly_count: 2 })} />);
    expect(screen.getAllByTestId('anomaly-card')).toHaveLength(2);
  });

  it('renders the summary bar', () => {
    render(<AnomalyReport anomalies={[makeAnomaly()]} billSummary={makeSummary()} />);
    expect(screen.getByTestId('summary-bar')).toBeInTheDocument();
  });

  it('highlights card when its code is in highlightCodes', () => {
    const codes: ReadonlySet<string> = new Set(['99213']);
    render(
      <AnomalyReport
        anomalies={[makeAnomaly()]}
        billSummary={makeSummary()}
        highlightCodes={codes}
      />,
    );
    const card = screen.getByTestId('anomaly-card');
    expect(card).toHaveClass('ring-2');
  });

  it('does not highlight card when code not in highlightCodes', () => {
    const codes: ReadonlySet<string> = new Set(['36415']);
    render(
      <AnomalyReport
        anomalies={[makeAnomaly()]}
        billSummary={makeSummary()}
        highlightCodes={codes}
      />,
    );
    const card = screen.getByTestId('anomaly-card');
    expect(card).not.toHaveClass('ring-2');
  });
});
