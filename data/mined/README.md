# Mined dataset (approved Mumbai residential plans)

Derived from the 50 municipal-approved projects in `Residential_Projects_28_to_77/` (main branch), 655 vector-PDF pages.

* `rooms_ocr_v1.jsonl.gz` - OCR'd room labels with clear dimensions (`engine/tools/mine_mumbai_pdfs.py`).
* `topology_v1.jsonl.gz` - per-flat room graphs: rooms, areas, neighbours, door/open links, façade contact,
  entrance (`engine/tools/extract_pdf_topology.py`). Quality audit: `docs/DATASET_VALIDATION.md`.

Kept in the repo so priors can be rebuilt without re-running OCR (about 1 hour).
