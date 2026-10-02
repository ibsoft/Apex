"""Standalone faster-whisper transcription for SIP calls.

Run by a *different* interpreter than the backend's: faster-whisper pulls
torch, which does not belong in APEX's dependency set, so it lives in its own
virtualenv (see SIP_WHISPER_PYTHON). That has one hard rule, which is why this
is a separate file rather than a function in sip_tools.py: **nothing here may
import anything from APEX**, because the interpreter running it cannot see the
backend on sys.path.

Only the transcript goes to stdout, and the first line of stderr on failure, so
the caller can use the output directly without parsing.

    .venv/sip-whisper/bin/python backend/tools/sip_whisper.py call.wav \
        --model tiny --language en
"""
from __future__ import annotations

import argparse
import sys


def transcribe(path: str, model: str, language: str = "", device: str = "cpu",
               compute_type: str = "int8") -> str:
    from faster_whisper import WhisperModel

    # Lazy import above: an ImportError has to arrive as a clean stderr line
    # with exit code 1, because the caller reports it to a person.
    model_obj = WhisperModel(model, device=device, compute_type=compute_type)
    segments, _info = model_obj.transcribe(
        path,
        language=language or None,
        vad_filter=True,
        # A phone line is narrowband. beam_size=1 (greedy) is several times
        # faster on short clips and loses almost nothing on clean speech.
        beam_size=1,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe one audio file with faster-whisper.")
    parser.add_argument("audio", help="Path to a WAV file.")
    parser.add_argument("--model", default="tiny", help="Model name or local path.")
    parser.add_argument("--language", default="", help="Language code, e.g. en or el. Empty auto-detects.")
    parser.add_argument("--device", default="cpu", help="cpu or cuda.")
    parser.add_argument("--compute-type", default="int8",
                        help="Quantisation, e.g. int8, float16.")
    args = parser.parse_args(argv)

    try:
        text = transcribe(args.audio, args.model, args.language,
                          args.device, args.compute_type)
    except ImportError as exc:
        print(f"faster-whisper is not installed in this interpreter: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())