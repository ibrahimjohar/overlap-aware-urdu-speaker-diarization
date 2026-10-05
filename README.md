# Urdu Speaker Diarization & Speaker-Attributed Transcription (FYP)

FAST-NUCES Karachi. Team: Areeba Hasnain (lead), Syed Ukkashah Ahmed, Ibrahim Johar Farooqi.
Supervisors: Dr. Muhammad Yaseen Khan, Dr. Muhammad Rafi (co-supervisor).

## Layout
| Folder | Contents |
|---|---|
| `docs/` | proposal, sourcing protocol, meeting notes, sign-offs |
| `sourcing/` | YouTube sourcing pipeline (s01-s12) and its config |
| `annotation/` | annotation guidelines and converters |
| `modeling/` | model code and training configs |
| `experiments/` | small result tables, plots, run configs |
| `report/` | LaTeX source of the report / paper |

## Data lives OUTSIDE this repo
Audio, the provenance database and checkpoints are never committed.
Each member sets the data location in a local `.env` (copy `.env.example`):

    URDU_CORPUS_ROOT=<data-root>/pilot/corpus   # prototype
    URDU_CORPUS_ROOT=<data-root>/main/corpus    # full run

Load it before running scripts (Git Bash / Linux / Mac):

    set -a; source .env; set +a

## Rules
- Never commit audio, databases, model weights or `.env`.
- Run sourcing scripts from `sourcing/`.
