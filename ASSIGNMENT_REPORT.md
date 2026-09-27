# Final technical report

The final technical report is written in LaTeX:

- **PDF:** [`report/main.pdf`](report/main.pdf)
- **Source:** [`report/main.tex`](report/main.tex). Build it with `tectonic main.tex`, or run
  `xelatex main.tex` twice.
- **Figures:** regenerate them from saved results with
  `python report/figures/make_figures.py`.

**Contents:**
- requirements traceability;
- high-level design: architecture, data flow, request sequence, data model and design
  decisions;
- methodology, with equations and the candidate-ranking algorithm;
- implementation;
- the Hiware Bazar case study with five ranked ponds;
- verification and validation: unit tests, rainfall vs IMD, DEM vs summits, land-cover
  accuracy and sensitivity analysis;
- software quality, limitations and references;
- appendices: installation guide and API reference.

Supporting documents:
- `README.md`: overview, installation and API.
- `VALIDATION.md`: validation details.
- `STUDY_GUIDE.md`: how every component works, likely questions and a demo script.
