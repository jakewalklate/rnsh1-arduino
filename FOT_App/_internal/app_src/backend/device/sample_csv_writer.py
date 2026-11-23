import csv
from pathlib import Path

from backend.config import RAW_MEASUREMENT_DATA_DIR


CSV_FIELDNAMES = [
    "sample",
    "time_s",
    "idx",
    "pressure_cmH2O",
    "flow_Lps",
]


def get_measurement_csv_path(measurement_id, data_dir=RAW_MEASUREMENT_DATA_DIR):
    return Path(data_dir) / f"{measurement_id}.csv"


class MeasurementCsvWriter:
    def __init__(self, measurement_id, data_dir=RAW_MEASUREMENT_DATA_DIR):
        self.measurement_id = measurement_id
        self.data_dir = Path(data_dir)
        self.file_path = get_measurement_csv_path(measurement_id, self.data_dir)
        self._file = None
        self._writer = None

    def open(self):
        if self._file is not None:
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.file_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_FIELDNAMES)
        self._writer.writeheader()
        self._file.flush()

    def write_rows(self, rows):
        if not rows:
            return

        self.open()

        for row in rows:
            self._writer.writerow({
                "sample": row.get("sample_number"),
                "time_s": _format_float(row.get("elapsed_seconds"), decimals=9),
                "idx": row.get("arduino_index"),
                "pressure_cmH2O": _format_float(
                    row.get("pressure_cmH2O"),
                    decimals=6,
                ),
                "flow_Lps": _format_float(row.get("flow_lps"), decimals=6),
            })

    def flush(self):
        if self._file is not None:
            self._file.flush()

    def close(self):
        if self._file is not None:
            self._file.flush()
            self._file.close()

        self._file = None
        self._writer = None


def _format_float(value, decimals=4):
    if value is None:
        return ""

    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return ""
