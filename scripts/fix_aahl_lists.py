import re

import common
import extract_questions as eq

for name, ypred in (("aahl", lambda y: y <= 2023),
                    ("aahl_b", lambda y: y >= 2024)):
    lines = []
    with open(common.MASTER_LIST, encoding="utf-8", errors="replace") as f:
        for line in f:
            ls = line.rstrip()
            if not ls.lower().endswith(".pdf"):
                continue
            fn = ls.replace("/", "\\").rsplit("\\", 1)[-1]
            if eq.classify_subject(fn) != "Mathematics_AAHL":
                continue
            m = re.search(r"(\d{4}) Examination Session", ls)
            if m and ypred(int(m.group(1))):
                lines.append(ls)
    with open(common.LISTS / ("list_%s.txt" % name), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(name, len(lines), "lines")
