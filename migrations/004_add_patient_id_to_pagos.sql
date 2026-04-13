-- BUG-014: Propagate patient_id (RUT del paciente DICOM) to payment tables
-- Allows correlating payments with the specific patient scan that triggered the charge.

ALTER TABLE pagos_tbk ADD COLUMN IF NOT EXISTS patient_id TEXT;
ALTER TABLE pagos_mp  ADD COLUMN IF NOT EXISTS patient_id TEXT;
