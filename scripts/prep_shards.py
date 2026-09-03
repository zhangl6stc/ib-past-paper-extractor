"""Generate per-shard list files (subject split via the pipeline's own
classifier, plus year ranges) so the full run can be parallelized."""
import re

import common
import extract_questions as eq

SHARDS = [
    ("cs_a",     "Computer_Science", lambda y: y <= 2015),
    ("cs_b",     "Computer_Science", lambda y: 2016 <= y <= 2020),
    ("cs_c",     "Computer_Science", lambda y: y >= 2021),
    ("econ_a",   "Economics_HL",     lambda y: y <= 2018),
    ("econ_b",   "Economics_HL",     lambda y: y >= 2019),
    ("phys_a",   "Physics_SL",       lambda y: y <= 2018),
    ("phys_b",   "Physics_SL",       lambda y: y >= 2019),
    ("mathhl_a", "Mathematics_HL",   lambda y: y <= 2015),
    ("mathhl_b", "Mathematics_HL",   lambda y: y >= 2016),
    ("aahl",     "Mathematics_AAHL", lambda y: True),
]

def main():
    for name, subject, ypred in SHARDS:
        lines = []
        with open(common.MASTER_LIST, encoding="utf-8", errors="replace") as f:
            for line in f:
                ls = line.rstrip()
                if not ls.lower().endswith(".pdf"):
                    continue
                fn = ls.replace("/", "\\").rsplit("\\", 1)[-1]
                if eq.classify_subject(fn) != subject:
                    continue
                m = re.search(r"(\d{4}) Examination Session", ls)
                if m and ypred(int(m.group(1))):
                    lines.append(ls)
        with open(common.LISTS / ("list_%s.txt" % name), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("%-9s %4d lines" % (name, len(lines)))


if __name__ == "__main__":
    main()
