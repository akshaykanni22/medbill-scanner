/**
 * __tests__/BillUploader.test.tsx
 * Tests for frontend/src/components/BillUploader.tsx
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BillUploader } from '../components/BillUploader';

// ---- Helpers ----

function makePdfFile(name = 'bill.pdf'): File {
  return new File(['%PDF-1.4 fake content'], name, { type: 'application/pdf' });
}

function makeJpegFile(name = 'bill.jpg'): File {
  return new File(['fake jpeg'], name, { type: 'image/jpeg' });
}

function makeHtmlFile(name = 'evil.html'): File {
  return new File(['<html>evil</html>'], name, { type: 'text/html' });
}

function makeOversizedFile(name = 'huge.pdf'): File {
  // 11 MB
  const bytes = new Uint8Array(11 * 1024 * 1024);
  return new File([bytes], name, { type: 'application/pdf' });
}

// ---- Tests ----

describe('BillUploader', () => {
  it('renders the drop zone', () => {
    render(<BillUploader onUpload={jest.fn()} />);
    expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument();
  });

  it('renders the Analyze Bill button', () => {
    render(<BillUploader onUpload={jest.fn()} />);
    expect(screen.getByTestId('analyze-button')).toBeInTheDocument();
  });

  it('Analyze Bill button is disabled when no file selected', () => {
    render(<BillUploader onUpload={jest.fn()} />);
    expect(screen.getByTestId('analyze-button')).toBeDisabled();
  });

  it('enables Analyze Bill button after a valid file is selected', async () => {
    const user = userEvent.setup();
    render(<BillUploader onUpload={jest.fn()} />);

    const input = screen.getByRole('bill-file-input', { hidden: true }) as HTMLInputElement
      || document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, makePdfFile());

    expect(screen.getByTestId('analyze-button')).not.toBeDisabled();
  });

  it('shows validation error for unsupported MIME type', async () => {
    render(<BillUploader onUpload={jest.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, makeHtmlFile());

    expect(screen.getByTestId('upload-error')).toBeInTheDocument();
    expect(screen.getByTestId('upload-error').textContent).toMatch(/not a supported/i);
  });

  it('shows validation error for oversized file', async () => {
    render(<BillUploader onUpload={jest.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, makeOversizedFile());

    expect(screen.getByTestId('upload-error')).toBeInTheDocument();
    expect(screen.getByTestId('upload-error').textContent).toMatch(/exceeds the/i);
  });

  it('keeps Analyze button disabled when invalid file is selected', async () => {
    render(<BillUploader onUpload={jest.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, makeHtmlFile());

    expect(screen.getByTestId('analyze-button')).toBeDisabled();
  });

  it('clears validation error when a valid file is selected after an invalid one', async () => {
    render(<BillUploader onUpload={jest.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, makeHtmlFile());
    expect(screen.getByTestId('upload-error')).toBeInTheDocument();

    await userEvent.upload(input, makePdfFile());
    expect(screen.queryByTestId('upload-error')).not.toBeInTheDocument();
  });

  it('calls onUpload with the file when form is submitted', async () => {
    const onUpload = jest.fn();
    render(<BillUploader onUpload={onUpload} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    const file = makePdfFile();
    await userEvent.upload(input, file);
    fireEvent.submit(screen.getByTestId('bill-uploader'));

    expect(onUpload).toHaveBeenCalledWith(file);
  });

  it('accepts JPEG files', async () => {
    render(<BillUploader onUpload={jest.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, makeJpegFile());

    expect(screen.queryByTestId('upload-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('analyze-button')).not.toBeDisabled();
  });

  it('shows the selected filename after selection', async () => {
    render(<BillUploader onUpload={jest.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, makePdfFile('my-bill.pdf'));

    expect(screen.getByText('my-bill.pdf')).toBeInTheDocument();
  });

  it('Analyze Bill button is disabled when disabled prop is true', async () => {
    const onUpload = jest.fn();
    render(<BillUploader onUpload={onUpload} disabled={true} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, makePdfFile());

    expect(screen.getByTestId('analyze-button')).toBeDisabled();
  });

  it('drop zone accepts a dragged file', () => {
    render(<BillUploader onUpload={jest.fn()} />);
    const dropZone = screen.getByRole('button', { name: /upload/i });
    const file = makePdfFile();

    fireEvent.drop(dropZone, {
      dataTransfer: {
        files: [file],
        types: ['Files'],
      },
    });

    // After a valid drop, the filename should be shown
    expect(screen.getByText('bill.pdf')).toBeInTheDocument();
  });

  it('shows error for multiple files dropped', () => {
    render(<BillUploader onUpload={jest.fn()} />);
    const dropZone = screen.getByRole('button', { name: /upload/i });

    fireEvent.drop(dropZone, {
      dataTransfer: {
        files: [makePdfFile(), makePdfFile('second.pdf')],
        types: ['Files'],
      },
    });

    expect(screen.getByTestId('upload-error')).toBeInTheDocument();
    expect(screen.getByTestId('upload-error').textContent).toMatch(/one file/i);
  });
});
