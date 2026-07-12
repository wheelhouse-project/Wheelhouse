"""STT Usage Metrics - CSV-based tracking for Google STT API costs.

This module provides simple CSV logging for each utterance processed by the STT
server, allowing analysis of API costs and noise-triggered requests.

Usage:
    metrics = UsageMetrics("stt_usage.csv")
    metrics.log_utterance(
        utterance_id=42,
        result_type="GOOGLE_FINAL",
        billed_seconds=9,
        word_count=5
    )
"""
import csv
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from shared_stt.redact import redact_transcript

logger = logging.getLogger("GoogleSTT")


class UsageMetrics:
    """Thread-safe CSV logger for STT usage metrics."""

    CSV_HEADERS = [
        "timestamp",
        "utterance_id",
        "result_type",
        "billed_seconds",
        "word_count",
        "text",
    ]

    def __init__(self, csv_path: str | None = None):
        """Initialize the usage metrics logger.

        Args:
            csv_path: Path to CSV file. Defaults to stt_usage.csv in same directory.
        """
        if csv_path is None:
            csv_path = str(Path(__file__).parent / "stt_usage.csv")
        
        self.csv_path = csv_path
        self._lock = threading.Lock()
        self._ensure_csv_exists()

    def _ensure_csv_exists(self):
        """Create CSV with headers if it doesn't exist."""
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)
            logger.info(f"[metrics] Created usage CSV: {self.csv_path}")

    def log_utterance(
        self,
        utterance_id: int,
        result_type: str,
        billed_seconds: int,
        word_count: int,
        text: str = "",
    ) -> None:
        """Log a single utterance to the CSV file.

        Args:
            utterance_id: The utterance ID from UtteranceManager
            result_type: Finalization reason (GOOGLE_FINAL, EOS_FALLBACK, GOOGLE_SILENCE_2S)
            billed_seconds: Seconds billed by Google (from total_billed_time or estimated)
            word_count: Number of words in the final text (0 if no text)
            text: The actual transcribed text
        """
        now = datetime.now()
        row = [
            now.isoformat(timespec="seconds"),
            utterance_id,
            result_type,
            billed_seconds,
            word_count,
            # Release default: the CSV keeps duration/latency metrics but
            # not content (wh-transcript-log-defaults).
            redact_transcript(text),
        ]

        with self._lock:
            try:
                with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(row)
            except Exception as e:
                logger.info(f"[metrics] Error writing to CSV: {e}")

        # Log summary for visibility
        status = "success" if word_count > 0 else "noise"
        text_preview = redact_transcript(text)
        logger.info(f"[metrics] UTT-{utterance_id}: {result_type}, {billed_seconds}s billed, {status}, text='{text_preview}'")
