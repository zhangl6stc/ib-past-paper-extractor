"""Central path resolution for the past-papers pipeline.

Every script lives in scripts/ and references data files through the
constants below (absolute paths), so the pipeline can be launched from any
working directory. Importing this module also puts scripts/ on sys.path so
sibling imports (extract_questions, prep_shards) resolve without a
`sys.path.insert(0, ".")` shim.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent

LISTS = ROOT / "lists"
MANIFESTS = ROOT / "manifests"
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"
AUDITS = ROOT / "audits"
REFERENCE = ROOT / "reference"

MASTER_LIST = LISTS / "by_year_file_list.txt"
MERGED_MANIFEST = MANIFESTS / "manifest.jsonl"
QUESTIONS_CSV = OUTPUT / "questions.csv"

# A fresh clone has none of the data directories yet; create them so
# scripts can write lists/, manifests/, output/, logs/ unconditionally.
for _dir in (LISTS, MANIFESTS, OUTPUT, LOGS, AUDITS):
    _dir.mkdir(parents=True, exist_ok=True)

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
