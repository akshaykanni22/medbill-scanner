/**
 * frontend/src/components/BillUploader.tsx
 * ============================================================
 * PURPOSE:
 *   Drag-and-drop file upload area for medical bills.
 *   Accepts PDF, JPEG, and PNG files up to 10 MB.
 *   Validates file type and size client-side before calling
 *   onUpload — catches obvious mistakes before hitting the backend.
 *
 * PROPS:
 *   onUpload(file) — called with the validated File when user submits
 *   disabled       — true while analysis is in progress (blocks re-upload)
 *
 * CLIENT-SIDE VALIDATION:
 *   Checks MIME type via file.type and size via file.size.
 *   This is UX-only — the backend re-validates with magic bytes.
 *   Never rely on frontend validation for security.
 *
 * PRIVACY NOTE (shown to user):
 *   A brief note explains that the file is processed locally and
 *   patient info is stripped before any AI analysis. This is accurate
 *   per the backend pipeline and builds patient trust.
 * ============================================================
 */

import React, { useCallback, useRef, useState } from 'react';

// ============================================================
// TYPES
// ============================================================

interface BillUploaderProps {
  /** Called with the selected file when the user clicks Analyze. */
  onUpload: (file: File) => void;
  /** When true, the uploader is disabled (analysis in progress). */
  disabled?: boolean;
}

// ============================================================
// CONSTANTS
// ============================================================

/** Must match backend MAX_UPLOAD_SIZE_MB setting. */
const MAX_SIZE_MB = 10;
const MAX_SIZE_BYTES = MAX_SIZE_MB * 1024 * 1024;

const ACCEPTED_MIME_TYPES = new Set(['application/pdf', 'image/jpeg', 'image/png']);
const ACCEPTED_EXTENSIONS = '.pdf,.jpg,.jpeg,.png';

// ============================================================
// HELPERS
// ============================================================

/**
 * Client-side file validation — UX guard only, not a security check.
 * Returns an error string or null if the file is acceptable.
 */
function validateFile(file: File): string | null {
  if (!ACCEPTED_MIME_TYPES.has(file.type)) {
    return `"${file.name}" is not a supported file type. Please upload a PDF, JPEG, or PNG.`;
  }
  if (file.size > MAX_SIZE_BYTES) {
    const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
    return `"${file.name}" is ${sizeMb} MB, which exceeds the ${MAX_SIZE_MB} MB limit.`;
  }
  return null;
}

// ============================================================
// COMPONENT
// ============================================================

/**
 * Drag-and-drop file upload area.
 *
 * DRAG STATE:
 *   isDragging is set true on dragenter/dragover, false on dragleave/drop.
 *   WHY SEPARATE FROM disabled: drag visual feedback is independent of
 *   whether the component is disabled — we still want to show the drag
 *   highlight if the user accidentally drags over a disabled uploader,
 *   so they get feedback rather than a confusing silent rejection.
 *   Actually, when disabled, we suppress drag feedback too (cleaner UX).
 *
 * FILE SELECTION:
 *   Both drag-and-drop and click-to-browse are supported.
 *   The hidden <input type="file"> is programmatically clicked via a ref.
 *
 * ACCESSIBILITY:
 *   The drop zone is a <button> so it's keyboard focusable and activatable
 *   with Enter/Space. Screen readers announce it as a button with a
 *   descriptive label.
 */
export function BillUploader({ onUpload, disabled = false }: BillUploaderProps): React.ReactElement {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);

  const inputRef = useRef<HTMLInputElement>(null);

  // ---- File selection ----

  const handleFileSelect = useCallback((file: File) => {
    const error = validateFile(file);
    if (error) {
      setValidationError(error);
      setSelectedFile(null);
      return;
    }
    setValidationError(null);
    setSelectedFile(file);
  }, []);

  // ---- Drag handlers ----

  const handleDragOver = useCallback(
    (e: React.DragEvent<HTMLButtonElement>) => {
      e.preventDefault();
      if (!disabled) setIsDragging(true);
    },
    [disabled],
  );

  const handleDragLeave = useCallback(
    (e: React.DragEvent<HTMLButtonElement>) => {
      e.preventDefault();
      // Only clear isDragging if the cursor left the button entirely.
      // When moving over a child element, relatedTarget is a descendant —
      // currentTarget.contains() returns true and we do nothing.
      if (!e.currentTarget.contains(e.relatedTarget as Node)) {
        setIsDragging(false);
      }
    },
    [],
  );

  const handleDragEnter = useCallback(
    (e: React.DragEvent<HTMLButtonElement>) => {
      e.preventDefault();
      if (!disabled) {
        setIsDragging(true);
        setValidationError(null); // clear stale error when new drag starts
      }
    },
    [disabled],
  );

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLButtonElement>) => {
      e.preventDefault();
      setIsDragging(false);
      if (disabled) return;
      if (e.dataTransfer.files.length > 1) {
        setValidationError('Please drop one file at a time.');
        return;
      }
      const file = e.dataTransfer.files[0];
      if (file) handleFileSelect(file);
    },
    [disabled, handleFileSelect],
  );

  // ---- Input change ----

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) handleFileSelect(file);
      // Reset input value so re-selecting the same file triggers onChange.
      if (inputRef.current) inputRef.current.value = '';
    },
    [handleFileSelect],
  );

  // ---- Submit ----

  const handleSubmit = useCallback(
    (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (selectedFile && !disabled) {
        onUpload(selectedFile);
      }
    },
    [selectedFile, disabled, onUpload],
  );

  // ---- Drop zone click (open file picker) ----

  const handleDropZoneClick = useCallback(() => {
    if (!disabled) inputRef.current?.click();
  }, [disabled]);

  // ---- Derived styles ----

  const dropZoneClasses = [
    'w-full rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors',
    'focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2',
    isDragging
      ? 'border-blue-500 bg-blue-50'
      : 'border-gray-300 bg-gray-50 hover:border-blue-400 hover:bg-blue-50',
    disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <form onSubmit={handleSubmit} noValidate data-testid="bill-uploader">
      {/* Drop zone */}
      <button
        type="button"
        className={dropZoneClasses}
        onClick={handleDropZoneClick}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        disabled={disabled}
        aria-label="Upload your medical bill. Click to browse or drag and drop a file here."
      >
        {/* Upload icon */}
        <svg
          className="mx-auto mb-4 h-12 w-12 text-gray-400"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={1.5}
          stroke="currentColor"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"
          />
        </svg>

        {selectedFile ? (
          <div>
            <span className="block text-sm font-semibold text-blue-700">{selectedFile.name}</span>
            <span className="block mt-1 text-xs text-gray-500">
              {(selectedFile.size / (1024 * 1024)).toFixed(1)} MB — click to change
            </span>
          </div>
        ) : (
          <div>
            <span className="block text-sm font-semibold text-gray-700">
              Drop your bill here, or{' '}
              <span className="text-blue-600 underline">browse</span>
            </span>
            <span className="block mt-1 text-xs text-gray-500">PDF, JPEG, or PNG — up to {MAX_SIZE_MB} MB</span>
          </div>
        )}
      </button>

      {/* Hidden file input */}
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED_EXTENSIONS}
        className="sr-only"
        onChange={handleInputChange}
        aria-hidden="true"
        tabIndex={-1}
      />

      {/* Validation error */}
      {validationError && (
        <p
          className="mt-2 text-sm text-red-600"
          role="alert"
          data-testid="upload-error"
        >
          {validationError}
        </p>
      )}

      {/* Privacy note */}
      <p className="mt-3 text-center text-xs text-gray-400">
        Your file is sent to the local analyzer running on your machine.
        Patient information is stripped before any AI processing — your
        bill data is never stored or sent to third parties.
      </p>

      {/* Submit button */}
      <button
        type="submit"
        disabled={!selectedFile || disabled}
        className="mt-4 w-full rounded-lg bg-blue-600 px-4 py-3 text-sm font-semibold
                   text-white shadow-sm transition-colors
                   hover:bg-blue-700 focus:outline-none focus:ring-2
                   focus:ring-blue-500 focus:ring-offset-2
                   disabled:cursor-not-allowed disabled:opacity-50"
        data-testid="analyze-button"
      >
        Analyze Bill
      </button>
    </form>
  );
}
