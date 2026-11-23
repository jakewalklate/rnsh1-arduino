import sqlite3
from datetime import datetime
from pathlib import Path

from backend.config import RAW_MEASUREMENT_DATA_DIR

DB_NAME = Path(__file__).resolve().parents[2] / "system.db"
DATETIME_FORMAT = "%d/%m/%Y %H:%M:%S"


def delete_measurement_csv_files(measurement_ids):
    for measurement_id in measurement_ids:
        measurement_csv_path = Path(RAW_MEASUREMENT_DATA_DIR) / f"{measurement_id}.csv"
        if measurement_csv_path.exists():
            measurement_csv_path.unlink()

# Get a database connection
def get_connection():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

# Initialize database
def initialize_database():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            patient_id TEXT PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            dob TEXT NOT NULL,
            sex TEXT NOT NULL,
            height INTEGER NOT NULL,
            weight INTEGER NOT NULL,
            smoking_status TEXT NOT NULL,
            pack_years REAL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            test_id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL,
            created_datetime TEXT NOT NULL,
            completed_datetime TEXT,
            frequency_hz REAL NOT NULL,
            sample_rate_hz REAL NOT NULL,
            duration_seconds REAL,
            recording_sessions INTEGER DEFAULT 0,
            status TEXT NOT NULL,

            FOREIGN KEY(patient_id)
            REFERENCES patients(patient_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS measurements (
            measurement_id TEXT PRIMARY KEY,
            test_id TEXT NOT NULL,
            measurement_number INTEGER NOT NULL,
            started_datetime TEXT,
            ended_datetime TEXT,
            duration_seconds REAL,
            status TEXT NOT NULL,

            FOREIGN KEY(test_id)
            REFERENCES tests(test_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS measurement_samples (
            sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
            measurement_id TEXT NOT NULL,
            sample_number INTEGER NOT NULL,
            elapsed_seconds REAL,
            arduino_index INTEGER,
            pressure_cmH2O REAL,
            flow_lps REAL,
            raw_line TEXT,
            created_datetime TEXT NOT NULL,

            FOREIGN KEY(measurement_id)
            REFERENCES measurements(measurement_id)
            ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS measurement_results (
            measurement_id TEXT PRIMARY KEY,
            resistance_R5 REAL,
            reactance_X5 REAL,
            impedance_Z5 REAL,
            resonant_frequency_hz REAL,
            ax REAL,
            tidal_volume_l REAL,
            sample_count INTEGER,
            raw_csv_path TEXT,
            status TEXT NOT NULL,
            created_datetime TEXT NOT NULL,

            FOREIGN KEY(measurement_id)
            REFERENCES measurements(measurement_id)
            ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS measurement_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            measurement_id TEXT NOT NULL,
            elapsed_seconds REAL NOT NULL,
            note TEXT NOT NULL,
            created_datetime TEXT NOT NULL,

            FOREIGN KEY(measurement_id)
            REFERENCES measurements(measurement_id)
            ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS test_parameters (
            test_id TEXT PRIMARY KEY,
            waveform_preset TEXT NOT NULL,
            niv_mode TEXT NOT NULL,
            ipap_cmH2O REAL NOT NULL,
            epap_cmH2O REAL NOT NULL,
            cpap_pressure_cmH2O REAL DEFAULT 5.0,
            max_duration_seconds INTEGER,
            updated_datetime TEXT NOT NULL,

            FOREIGN KEY(test_id)
            REFERENCES tests(test_id)
        )
    """)

    ensure_test_parameter_columns(cursor)
    ensure_measurement_columns(cursor)
    ensure_measurement_result_columns(cursor)

    conn.commit()
    conn.close()


def ensure_measurement_columns(cursor):
    """CPAP is stamped onto the MEASUREMENT, not just the test.

    test_parameters holds one row per test and is overwritten in place, so a test whose
    measurements were taken at different pressures keeps only the last one. TEST0009 is
    exactly that case: MEAS0012 was recorded under CPAP and MEAS0013 with the tube open
    to air, and the test row reads 0.0 for both. NULL means the measurement predates
    this column and its pressure was not recorded."""
    cursor.execute("PRAGMA table_info(measurements)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    if "cpap_pressure_cmH2O" not in existing_columns:
        cursor.execute("""
            ALTER TABLE measurements
            ADD COLUMN cpap_pressure_cmH2O REAL
        """)


def ensure_test_parameter_columns(cursor):
    cursor.execute("PRAGMA table_info(test_parameters)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    if "cpap_pressure_cmH2O" not in existing_columns:
        cursor.execute("""
            ALTER TABLE test_parameters
            ADD COLUMN cpap_pressure_cmH2O REAL DEFAULT 5.0
        """)

    if "max_duration_seconds" not in existing_columns:
        cursor.execute("""
            ALTER TABLE test_parameters
            ADD COLUMN max_duration_seconds INTEGER
        """)


# Within-breath columns, added after the table shipped. CREATE TABLE IF NOT EXISTS
# leaves an existing database on its old schema, so they are added here instead.
# Measurements saved before this stay readable with NULLs in the new columns.
WITHIN_BREATH_RESULT_COLUMNS = (
    ("Rrs_insp", "REAL"),
    ("Rrs_exp", "REAL"),
    ("dRrs", "REAL"),
    ("Xrs_insp", "REAL"),
    ("Xrs_exp", "REAL"),
    ("dXrs", "REAL"),
    ("cov_r5", "REAL"),
    ("clean_breath_count", "INTEGER"),
    ("breath_count", "INTEGER"),
)


# The same measurement reported on the SCALE-only basis, i.e. with the bias-flow
# correction off. Stored alongside rather than recomputed on every view so a saved
# result stays fixed even if the calibration file is later replaced.
#
# ONLY the fields that actually differ are duplicated. dRrs and dXrs are identical on
# both bases because the additive term cancels between the two phases of a breath, and
# breath counts, quality and tidal volume are identical because QC runs upstream of the
# correction. Storing second copies of those would invite them drifting apart.
#
# `status_nobias` doubles as the "has this been computed" marker: NULL means the row
# predates this feature and the view backfills it on first use.
SCALE_ONLY_RESULT_COLUMNS = (
    ("resistance_R5_nobias", "REAL"),
    ("reactance_X5_nobias", "REAL"),
    ("impedance_Z5_nobias", "REAL"),
    ("Rrs_insp_nobias", "REAL"),
    ("Rrs_exp_nobias", "REAL"),
    ("Xrs_insp_nobias", "REAL"),
    ("Xrs_exp_nobias", "REAL"),
    ("cov_r5_nobias", "REAL"),
    ("status_nobias", "TEXT"),
)


def ensure_measurement_result_columns(cursor):
    cursor.execute("PRAGMA table_info(measurement_results)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    for name, column_type in WITHIN_BREATH_RESULT_COLUMNS + SCALE_ONLY_RESULT_COLUMNS:
        if name not in existing_columns:
            cursor.execute(
                f"ALTER TABLE measurement_results ADD COLUMN {name} {column_type}"
            )

# Generate a new patient ID
def generate_patient_id():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT patient_id
        FROM patients
    """)

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "PAT0001"

    max_id = 0

    for row in rows:
        try:
            num = int(row[0].replace("PAT", ""))
            max_id = max(max_id, num)
        except ValueError:
            pass

    return f"PAT{max_id + 1:04d}"

# Create patient
def create_patient(first_name, last_name, dob, sex, height, weight, smoking_status, pack_years):

    patient_id = generate_patient_id()

    first_name = first_name.strip().title()
    last_name = last_name.strip().title()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO patients (
            patient_id,
            first_name,
            last_name,
            dob,
            sex,
            height,
            weight,
            smoking_status,
            pack_years
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        patient_id,
        first_name,
        last_name,
        dob,
        sex,
        int(height),
        int(weight),
        smoking_status,
        pack_years
    ))

    conn.commit()
    conn.close()

    return patient_id

# Edit patient
def update_patient(
    patient_id,
    first_name,
    last_name,
    dob,
    sex,
    height,
    weight,
    smoking_status,
    pack_years
):

    first_name = first_name.strip().title()
    last_name = last_name.strip().title()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE patients
        SET
            first_name = ?,
            last_name = ?,
            dob = ?,
            sex = ?,
            height = ?,
            weight = ?,
            smoking_status = ?,
            pack_years = ?
        WHERE patient_id = ?
    """, (
        first_name,
        last_name,
        dob,
        sex,
        int(height),
        int(weight),
        smoking_status,
        pack_years,
        patient_id
    ))

    conn.commit()
    conn.close()

# Get all patients
def get_all_patients():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            patient_id,
            first_name,
            last_name,
            dob,
            sex,
            height,
            weight,
            smoking_status,
            pack_years
        FROM patients
        ORDER BY last_name, first_name
    """)

    patients = cursor.fetchall()

    conn.close()

    return patients

# Get patient by ID
def get_patient(patient_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            patient_id,
            first_name,
            last_name,
            dob,
            sex,
            height,
            weight,
            smoking_status,
            pack_years
        FROM patients
        WHERE patient_id = ?
    """, (patient_id,))

    patient = cursor.fetchone()

    conn.close()

    return patient

# Delete patient
def delete_patient(patient_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT patient_id
        FROM patients
        WHERE patient_id = ?
    """, (patient_id,))

    if cursor.fetchone() is None:
        conn.close()
        return False

    cursor.execute("""
        SELECT measurements.measurement_id
        FROM measurements
        INNER JOIN tests
            ON measurements.test_id = tests.test_id
        WHERE tests.patient_id = ?
    """, (patient_id,))

    measurement_ids = [row[0] for row in cursor.fetchall()]

    cursor.execute("""
        DELETE FROM measurement_samples
        WHERE measurement_id IN (
            SELECT measurements.measurement_id
            FROM measurements
            INNER JOIN tests
                ON measurements.test_id = tests.test_id
            WHERE tests.patient_id = ?
        )
    """, (patient_id,))

    cursor.execute("""
        DELETE FROM measurements
        WHERE test_id IN (
            SELECT test_id
            FROM tests
            WHERE patient_id = ?
        )
    """, (patient_id,))

    cursor.execute("""
        DELETE FROM test_parameters
        WHERE test_id IN (
            SELECT test_id
            FROM tests
            WHERE patient_id = ?
        )
    """, (patient_id,))

    cursor.execute("""
        DELETE FROM tests
        WHERE patient_id = ?
    """, (patient_id,))

    cursor.execute("""
        DELETE FROM patients
        WHERE patient_id = ?
    """, (patient_id,))

    conn.commit()
    conn.close()

    delete_measurement_csv_files(measurement_ids)

    return True

# Search for patient
def search_patients(query: str):

    conn = get_connection()
    cursor = conn.cursor()

    query = f"%{query.strip()}%"

    cursor.execute("""
        SELECT patient_id, first_name, last_name, dob, sex, height, weight, smoking_status, pack_years
        FROM patients
        WHERE
            patient_id LIKE ?
            OR first_name LIKE ?
            OR last_name LIKE ?
        ORDER BY last_name, first_name
    """, (query, query, query))

    results = cursor.fetchall()
    conn.close()

    return results

# Generate test ID
def generate_test_id():

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT test_id
        FROM tests
    """)

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "TEST0001"

    max_id = 0

    for row in rows:
        try:
            num = int(row[0].replace("TEST", ""))
            max_id = max(max_id, num)
        except ValueError:
            pass

    return f"TEST{max_id + 1:04d}"

def create_test(patient_id,
                frequency_hz=6,
                sample_rate_hz=420):

    test_id = generate_test_id()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO tests(
            test_id,
            patient_id,
            created_datetime,
            completed_datetime,
            frequency_hz,
            sample_rate_hz,
            duration_seconds,
            recording_sessions,
            status
        )

        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

    """, (

        test_id,
        patient_id,
        datetime.now().strftime(DATETIME_FORMAT),
        None,
        frequency_hz,
        sample_rate_hz,
        None,
        0,
        "Waiting"

    ))

    conn.commit()
    conn.close()

    return test_id


def save_test_parameters(
    test_id,
    cpap_pressure_cmH2O=5.0,
    max_duration_seconds=None
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO test_parameters (
            test_id,
            waveform_preset,
            niv_mode,
            ipap_cmH2O,
            epap_cmH2O,
            cpap_pressure_cmH2O,
            max_duration_seconds,
            updated_datetime
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(test_id) DO UPDATE SET
            cpap_pressure_cmH2O = excluded.cpap_pressure_cmH2O,
            max_duration_seconds = excluded.max_duration_seconds,
            updated_datetime = excluded.updated_datetime
    """, (
        test_id,
        "Clinical",
        "CPAP",
        10.0,
        5.0,
        float(cpap_pressure_cmH2O),
        max_duration_seconds,
        datetime.now().strftime(DATETIME_FORMAT)
    ))

    conn.commit()
    conn.close()


def get_test_parameters(test_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            cpap_pressure_cmH2O,
            max_duration_seconds
        FROM test_parameters
        WHERE test_id = ?
    """, (test_id,))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        save_test_parameters(test_id)
        return {
            "cpap_pressure_cmH2O": 5.0,
            "max_duration_seconds": None,
        }

    return {
        "cpap_pressure_cmH2O": row[0],
        "max_duration_seconds": row[1],
    }

# Get a test
def get_test(test_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM tests
        WHERE test_id = ?
    """, (test_id,))

    test = cursor.fetchone()

    conn.close()

    return test


def delete_test(test_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT patient_id
        FROM tests
        WHERE test_id = ?
    """, (test_id,))

    row = cursor.fetchone()

    if row is None:
        conn.close()
        return None

    patient_id = row[0]

    cursor.execute("""
        SELECT measurement_id
        FROM measurements
        WHERE test_id = ?
    """, (test_id,))

    measurement_ids = [measurement_row[0] for measurement_row in cursor.fetchall()]

    cursor.execute("""
        DELETE FROM measurement_samples
        WHERE measurement_id IN (
            SELECT measurement_id
            FROM measurements
            WHERE test_id = ?
        )
    """, (test_id,))

    cursor.execute("""
        DELETE FROM measurements
        WHERE test_id = ?
    """, (test_id,))

    cursor.execute("""
        DELETE FROM test_parameters
        WHERE test_id = ?
    """, (test_id,))

    cursor.execute("""
        DELETE FROM tests
        WHERE test_id = ?
    """, (test_id,))

    conn.commit()
    conn.close()

    delete_measurement_csv_files(measurement_ids)

    return patient_id


def delete_test_if_no_complete_measurements(test_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT patient_id
        FROM tests
        WHERE test_id = ?
    """, (test_id,))

    row = cursor.fetchone()

    if row is None:
        conn.close()
        return None

    cursor.execute("""
        SELECT COUNT(*)
        FROM measurements
        WHERE test_id = ?
          AND duration_seconds IS NOT NULL
          AND status = 'Complete'
    """, (test_id,))

    complete_measurement_count = cursor.fetchone()[0]
    conn.close()

    if complete_measurement_count > 0:
        return None

    return delete_test(test_id)


# Get all tests for a patient
def get_patient_tests(patient_id):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM tests
        WHERE patient_id = ?
        ORDER BY created_datetime DESC
    """, (patient_id,))

    tests = cursor.fetchall()

    conn.close()

    return tests

# Update test status
def update_test_status(test_id, status):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE tests
        SET status = ?
        WHERE test_id = ?
    """, (status, test_id))

    conn.commit()
    conn.close()

def complete_test(test_id,
                  duration_seconds,
                  recording_sessions):

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE tests

        SET

            completed_datetime = ?,
            duration_seconds = ?,
            recording_sessions = ?,
            status = ?

        WHERE test_id = ?

    """, (

        datetime.now().strftime(DATETIME_FORMAT),
        duration_seconds,
        recording_sessions,
        "Complete",
        test_id

    ))

    conn.commit()
    conn.close()

# Create measurement ID 
def generate_measurement_id():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT measurement_id FROM measurements")
    rows = cursor.fetchall()

    conn.close()

    if not rows:
        return "MEAS0001"

    max_id = 0

    for row in rows:
        try:
            num = int(row[0].replace("MEAS", ""))
            max_id = max(max_id, num)
        except ValueError:
            pass

    return f"MEAS{max_id + 1:04d}"

# Get next measurement number for a test
def get_next_measurement_number(test_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*)
        FROM measurements
        WHERE test_id = ?
    """, (test_id,))

    count = cursor.fetchone()[0]

    conn.close()

    return count + 1

def create_measurement(test_id, cpap_pressure_cmH2O=None):
    """`cpap_pressure_cmH2O` is the pressure in force when THIS measurement started.
    Recorded per measurement because the test-level parameter row is overwritten
    whenever the operator reconfigures, so it cannot describe a test whose measurements
    were taken at different pressures. See ensure_measurement_columns."""
    measurement_id = generate_measurement_id()
    measurement_number = get_next_measurement_number(test_id)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO measurements (
            measurement_id,
            test_id,
            measurement_number,
            started_datetime,
            ended_datetime,
            duration_seconds,
            status,
            cpap_pressure_cmH2O
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        measurement_id,
        test_id,
        measurement_number,
        datetime.now().strftime(DATETIME_FORMAT),
        None,
        None,
        "Recording",
        None if cpap_pressure_cmH2O is None else float(cpap_pressure_cmH2O),
    ))

    cursor.execute("""
        UPDATE tests
        SET status = ?
        WHERE test_id = ?
    """, (
        "Recording",
        test_id
    ))

    conn.commit()
    conn.close()

    return measurement_id

# Complete measurement when STOP
def get_saved_measurement_count(cursor, test_id):
    cursor.execute("""
        SELECT COUNT(*)
        FROM measurements
        WHERE test_id = ?
          AND duration_seconds IS NOT NULL
          AND status = 'Complete'
    """, (test_id,))

    return cursor.fetchone()[0]


def get_saved_measurement_duration(cursor, test_id):
    cursor.execute("""
        SELECT COALESCE(SUM(duration_seconds), 0)
        FROM measurements
        WHERE test_id = ?
          AND duration_seconds IS NOT NULL
          AND status = 'Complete'
    """, (test_id,))

    return cursor.fetchone()[0]


def complete_measurement(measurement_id, duration_seconds, ended_datetime=None):
    conn = get_connection()
    cursor = conn.cursor()

    if ended_datetime is None:
        ended_datetime = datetime.now()

    cursor.execute("""
        SELECT test_id
        FROM measurements
        WHERE measurement_id = ?
    """, (measurement_id,))

    row = cursor.fetchone()

    if row is None:
        conn.close()
        return False

    test_id = row[0]

    cursor.execute("""
        UPDATE measurements
        SET
            ended_datetime = ?,
            duration_seconds = ?,
            status = ?
        WHERE measurement_id = ?
    """, (
        ended_datetime.strftime(DATETIME_FORMAT),
        duration_seconds,
        "Complete",
        measurement_id
    ))

    saved_count = get_saved_measurement_count(cursor, test_id)
    total_duration = get_saved_measurement_duration(cursor, test_id)

    cursor.execute("""
        UPDATE tests
        SET
            completed_datetime = ?,
            duration_seconds = ?,
            recording_sessions = ?,
            status = ?
        WHERE test_id = ?
    """, (
        ended_datetime.strftime(DATETIME_FORMAT),
        total_duration,
        saved_count,
        "Complete",
        test_id
    ))

    conn.commit()
    conn.close()

    return True


def save_measurement_results(measurement_id, results):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM measurement_results
        WHERE measurement_id = ?
    """, (measurement_id,))

    cursor.execute("""
        INSERT INTO measurement_results (
            measurement_id,
            resistance_R5,
            reactance_X5,
            impedance_Z5,
            resonant_frequency_hz,
            ax,
            tidal_volume_l,
            sample_count,
            raw_csv_path,
            status,
            created_datetime,
            Rrs_insp,
            Rrs_exp,
            dRrs,
            Xrs_insp,
            Xrs_exp,
            dXrs,
            cov_r5,
            clean_breath_count,
            breath_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        measurement_id,
        results.get("resistance_R5"),
        results.get("reactance_X5"),
        results.get("impedance_Z5"),
        results.get("resonant_frequency_hz"),
        results.get("ax"),
        results.get("tidal_volume_l"),
        results.get("sample_count"),
        results.get("raw_csv_path"),
        results.get("status", "Pending"),
        datetime.now().strftime(DATETIME_FORMAT),
        results.get("Rrs_insp"),
        results.get("Rrs_exp"),
        results.get("dRrs"),
        results.get("Xrs_insp"),
        results.get("Xrs_exp"),
        results.get("dXrs"),
        results.get("cov_r5"),
        results.get("clean_breath_count"),
        results.get("breath_count"),
    ))

    conn.commit()
    conn.close()


def save_scale_only_results(measurement_id, results):
    """Write the bias-flow-correction-off copy of an existing result row.

    Separate from save_measurement_results because it is used twice: once at record
    time, and once lazily when an older measurement is viewed with the toggle for the
    first time. UPDATE rather than INSERT, so it can never create a half-populated row
    for a measurement that has no primary result."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE measurement_results
        SET
            resistance_R5_nobias = ?,
            reactance_X5_nobias = ?,
            impedance_Z5_nobias = ?,
            Rrs_insp_nobias = ?,
            Rrs_exp_nobias = ?,
            Xrs_insp_nobias = ?,
            Xrs_exp_nobias = ?,
            cov_r5_nobias = ?,
            status_nobias = ?
        WHERE measurement_id = ?
    """, (
        results.get("resistance_R5"),
        results.get("reactance_X5"),
        results.get("impedance_Z5"),
        results.get("Rrs_insp"),
        results.get("Rrs_exp"),
        results.get("Xrs_insp"),
        results.get("Xrs_exp"),
        results.get("cov_r5"),
        results.get("status", "Pending"),
        measurement_id,
    ))

    updated = cursor.rowcount
    conn.commit()
    conn.close()

    return updated > 0


def get_measurement_results(measurement_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            measurement_id,
            resistance_R5,
            reactance_X5,
            impedance_Z5,
            resonant_frequency_hz,
            ax,
            tidal_volume_l,
            sample_count,
            raw_csv_path,
            status,
            created_datetime,
            Rrs_insp,
            Rrs_exp,
            dRrs,
            Xrs_insp,
            Xrs_exp,
            dXrs,
            cov_r5,
            clean_breath_count,
            breath_count,
            resistance_R5_nobias,
            reactance_X5_nobias,
            impedance_Z5_nobias,
            Rrs_insp_nobias,
            Rrs_exp_nobias,
            Xrs_insp_nobias,
            Xrs_exp_nobias,
            cov_r5_nobias,
            status_nobias
        FROM measurement_results
        WHERE measurement_id = ?
    """, (measurement_id,))

    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    return {
        "measurement_id": row[0],
        "resistance_R5": row[1],
        "reactance_X5": row[2],
        "impedance_Z5": row[3],
        "resonant_frequency_hz": row[4],
        "ax": row[5],
        "tidal_volume_l": row[6],
        "sample_count": row[7],
        "raw_csv_path": row[8],
        "status": row[9],
        "created_datetime": row[10],
        # NULL for measurements saved before these columns existed, and for takes with
        # too few clean breaths to split into inspiration and expiration.
        "Rrs_insp": row[11],
        "Rrs_exp": row[12],
        "dRrs": row[13],
        "Xrs_insp": row[14],
        "Xrs_exp": row[15],
        "dXrs": row[16],
        "cov_r5": row[17],
        "clean_breath_count": row[18],
        "breath_count": row[19],
        # Bias-flow-correction-off copy. `status_nobias` is None for rows saved before
        # this existed, which is how the results view knows to backfill it. Only the
        # fields that genuinely differ are here; everything else is shared with the
        # bias-corrected result above. See SCALE_ONLY_RESULT_COLUMNS.
        "scale_only": {
            "resistance_R5": row[20],
            "reactance_X5": row[21],
            "impedance_Z5": row[22],
            "Rrs_insp": row[23],
            "Rrs_exp": row[24],
            "Xrs_insp": row[25],
            "Xrs_exp": row[26],
            "cov_r5": row[27],
            "status": row[28],
        },
    }


def save_measurement_event(measurement_id, elapsed_seconds, note):
    note = note.strip()

    if not note:
        return None

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO measurement_events (
            measurement_id,
            elapsed_seconds,
            note,
            created_datetime
        )
        VALUES (?, ?, ?, ?)
    """, (
        measurement_id,
        float(elapsed_seconds),
        note,
        datetime.now().strftime(DATETIME_FORMAT)
    ))

    event_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return event_id


def get_measurement_events(measurement_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            event_id,
            measurement_id,
            elapsed_seconds,
            note,
            created_datetime
        FROM measurement_events
        WHERE measurement_id = ?
        ORDER BY elapsed_seconds, event_id
    """, (measurement_id,))

    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "event_id": row[0],
            "measurement_id": row[1],
            "elapsed_seconds": row[2],
            "note": row[3],
            "created_datetime": row[4],
        }
        for row in rows
    ]


def discard_measurement(measurement_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT test_id
        FROM measurements
        WHERE measurement_id = ?
    """, (measurement_id,))

    row = cursor.fetchone()

    if row is None:
        conn.close()
        return False

    test_id = row[0]

    cursor.execute("""
        DELETE FROM measurement_samples
        WHERE measurement_id = ?
    """, (measurement_id,))

    cursor.execute("""
        DELETE FROM measurements
        WHERE measurement_id = ?
    """, (measurement_id,))

    saved_count = get_saved_measurement_count(cursor, test_id)
    total_duration = get_saved_measurement_duration(cursor, test_id)

    if saved_count:
        status = "Complete"
        completed_datetime = datetime.now().strftime(DATETIME_FORMAT)
        duration_seconds = total_duration
    else:
        status = "Waiting"
        completed_datetime = None
        duration_seconds = None

    cursor.execute("""
        UPDATE tests
        SET
            completed_datetime = ?,
            duration_seconds = ?,
            recording_sessions = ?,
            status = ?
        WHERE test_id = ?
    """, (
        completed_datetime,
        duration_seconds,
        saved_count,
        status,
        test_id
    ))

    conn.commit()
    conn.close()

    delete_measurement_csv_files([measurement_id])

    return True


def save_measurement_sample(
    measurement_id,
    sample_number,
    elapsed_seconds=None,
    arduino_index=None,
    pressure_cmH2O=None,
    flow_lps=None,
    raw_line=None
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO measurement_samples (
            measurement_id,
            sample_number,
            elapsed_seconds,
            arduino_index,
            pressure_cmH2O,
            flow_lps,
            raw_line,
            created_datetime
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        measurement_id,
        sample_number,
        elapsed_seconds,
        arduino_index,
        pressure_cmH2O,
        flow_lps,
        raw_line,
        datetime.now().strftime(DATETIME_FORMAT)
    ))

    conn.commit()
    conn.close()


def save_measurement_samples(measurement_id, sample_rows):
    rows = []
    created_datetime = datetime.now().strftime(DATETIME_FORMAT)

    for sample in sample_rows:
        rows.append((
            measurement_id,
            sample.get("sample_number"),
            sample.get("elapsed_seconds"),
            sample.get("arduino_index"),
            sample.get("pressure_cmH2O"),
            sample.get("flow_lps"),
            sample.get("raw_line"),
            created_datetime
        ))

    if not rows:
        return 0

    conn = get_connection()
    cursor = conn.cursor()

    cursor.executemany("""
        INSERT INTO measurement_samples (
            measurement_id,
            sample_number,
            elapsed_seconds,
            arduino_index,
            pressure_cmH2O,
            flow_lps,
            raw_line,
            created_datetime
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)

    conn.commit()
    conn.close()

    return len(rows)

# Get measurements for a test
def get_test_measurements(test_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            measurement_id,
            test_id,
            measurement_number,
            started_datetime,
            ended_datetime,
            duration_seconds,
            status,
            cpap_pressure_cmH2O
        FROM measurements
        WHERE test_id = ?
          AND duration_seconds IS NOT NULL
        ORDER BY measurement_number
    """, (test_id,))

    measurements = cursor.fetchall()

    conn.close()

    return measurements


