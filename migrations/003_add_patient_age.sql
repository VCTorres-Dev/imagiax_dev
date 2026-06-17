-- BUG-008: Add patient_age column to informes table
-- Zero-downtime migration: nullable column, no default, no constraint
ALTER TABLE informes ADD COLUMN IF NOT EXISTS patient_age VARCHAR(10) NULL;
COMMENT ON COLUMN informes.patient_age IS 'DICOM PatientAge (e.g. 045Y, 008M) or computed from PatientBirthDate';
