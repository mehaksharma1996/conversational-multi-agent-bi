"""Load uploaded CSV and Excel files into pandas dataframes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import pandas as pd

SUPPORTED_TABULAR_EXTENSIONS = {".csv", ".xls", ".xlsx"}


class TabularLoadError(ValueError):
    """Raised when an uploaded tabular file cannot be loaded."""


@dataclass(frozen=True)
class LoadedTable:
    dataframe: pd.DataFrame
    filename: str
    extension: str
    row_count: int
    column_count: int


def load_tabular_file(file: BinaryIO, filename: str) -> LoadedTable:
    """Load a CSV or Excel upload and return dataframe metadata."""
    extension = Path(filename).suffix.lower()

    if extension not in SUPPORTED_TABULAR_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_TABULAR_EXTENSIONS))
        raise TabularLoadError(
            f"Unsupported file type '{extension or 'unknown'}'. "
            f"Supported tabular types are: {supported}."
        )

    try:
        dataframe = _read_dataframe(file=file, extension=extension)
    except UnicodeDecodeError as exc:
        raise TabularLoadError(
            "Could not decode the CSV file. Please save it as UTF-8 and try again."
        ) from exc
    except Exception as exc:
        raise TabularLoadError(f"Could not load '{filename}': {exc}") from exc

    if dataframe.empty and len(dataframe.columns) == 0:
        raise TabularLoadError("The uploaded file did not contain any columns.")

    return LoadedTable(
        dataframe=dataframe,
        filename=filename,
        extension=extension,
        row_count=len(dataframe),
        column_count=len(dataframe.columns),
    )


def _read_dataframe(file: BinaryIO, extension: str) -> pd.DataFrame:
    file.seek(0)

    if extension == ".csv":
        return pd.read_csv(file)

    return pd.read_excel(file)
