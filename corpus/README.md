# Corpus

This folder holds what the system indexes. Nothing here is invented: every
passage must come from a document you downloaded.

| File | What goes in it |
| --- | --- |
| `raw/` | The PDFs: the EIA Notification, 2006 and the amendments / office memoranda you chose. Not committed (see `.gitignore`); each teammate downloads them. |
| `manifest.yaml` | One entry per PDF: title, S.O. number, date, URL, text layer yes/no, what it covers. |
| `clauses.yaml` | The Schedule rows for the sectors you support, and the General Condition, copied verbatim with page numbers. |

## Stage 1 checklist (Evaluation 1)

1. Download the EIA Notification, 2006 (S.O. 1533(E), 14 September 2006) from
   the MoEFCC website or the PARIVESH portal. Save it as
   `raw/eia_notification_2006.pdf`.
2. Check the text layer: if you can select text in the PDF, `text_layer: yes`.
   A scanned file needs OCR before ingestion will index it.
3. Pick the amendments to include, with a cutoff date. Prioritise any that
   change the General Condition or your test sector. Add each to the manifest.
4. Copy the Schedule row for each supported sector into `clauses.yaml`,
   verbatim, with the page number. Confirm the item numbers (e.g. 3(b)) match
   `ecs/contract.py`.
5. Copy the General Condition, current wording, into the `GC` entry.
6. Confirm the expected answers in `eval/scenarios.jsonl` against these texts.
7. Rebuild the index: `docker compose run --rm ingest` (only changed chunks are
   re-embedded).

The section 4.1 test values (cement below the Category A threshold, 2 km from
a national park boundary, expected Category A citing both 3(b) and the GC) are
scenario `s01_4_1_case`; contrasts are `s02` (15 km, B) and `s03` (unknown, escalate).
