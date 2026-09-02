# Green Vision AI
### Intel® AI Global Impact Festival · AI Changemaker · Stage 3 (Global Final)

---

## Project Title

**Green Vision AI**: the city map that shows where to plant trees before it's too late.

---

## Project Synopsis (about 150 words)

Green cover is disappearing fast in Indian cities and cities worldwide. It isn't just about looks: fewer trees means concrete traps more heat. Streets run up to 6°C hotter, India has counted over 6,100 heat deaths since 2010 (WRI, 2019), and 66 of the world's 100 most polluted cities are Indian (IQAir, 2025). Planting the right trees in the right places fights both problems at once. The roadblock is always the same: *where?*

Green Vision answers that. It merges 42 months of NASA satellite history with air quality, weather and soil data across 146 hexagonal cells of Ahmedabad, then predicts each cell's air quality within ±13.5 AQI points and its green cover within about 4% canopy a year ahead, tested on months the model never saw. Maps show the past. Green Vision predicts the future.

Proven in Ahmedabad and ready anywhere, since Delhi took one config file. It all runs on an ordinary laptop through Intel OpenVINO, on free public data.

---

## Created as part of an Intel® Program

**Yes, Intel® AI for Youth.** Its Responsible AI framework shaped the system's defining habit: every statistic is labelled MEASURED, MODELLED or PROJECTED, and every AI inference runs on the user's own machine.

---

## GenAI Tool Usage

The code was written with **Claude Code**, working under our direction, and we are fully transparent about that. Everything that makes the software worth having is our own: the idea and its framing, the hexagonal grid design, the selection and verification of every data source, the decision to make four AI approaches compete on one held-out test and deploy only the measured winner, the validation rules that limit what the system may call a prediction, and every accuracy figure on this form. We used GenAI the way we want city planners to use Green Vision: as a powerful tool whose output is verified, constrained, and owned by a human who understands it. We can explain and defend any part of this system.

GenAI is also part of the product itself, and not as a wrapper around a cloud service. A local language model (Qwen2.5, compressed to INT4, running through the Intel OpenVINO GenAI API with no cloud and no API key) writes a plain language justification for every recommended cell and selects three to five tree species matched to that cell's measured soil pH, texture and pollution load. Strict validation confines it to restating values the system has already computed: it cannot invent data, reorder the ranking, or fabricate a species. If its formatting ever fails, a deterministic writer takes over automatically, so a planner's run never fails because of a language model.

---

## Target Audience

Green Vision is built for the people who decide where trees go: municipal corporations, urban planning departments, and smart city cells. It arrives at exactly the right moment. In July 2026 the Ahmedabad Municipal Corporation launched **Mission Five Million Trees**, a public commitment to plant 50 lakh trees in 2026 to 27. The mission's hardest operational question is the one Green Vision answers: which areas first, and where inside them is the actual plantable ground, ranked by predicted decline and located to 10 metres using Sentinel-2 satellite imagery.

We are now placing the tool in the mission's hands. A live demonstration to the Ahmedabad Municipal Corporation's horticulture wing, and a pilot with a partner NGO that will site a real plantation drive using our priority map, are both **in progress**, with written statements to follow.

Using it requires no GIS training, no GPU and no cloud budget. The models are compressed with Intel's NNCF to run on an ordinary office laptop, every data source is free, and no information about the city ever leaves the machine, which matters greatly when the user is a government. Because the crews who plant the trees mostly do not read English, every analysis also produces its planting brief in **Hindi and Gujarati**, translated through carefully written templates rather than machine translation, since a wrong translation on a work order is worse than none.

The design is city agnostic. **Delhi became our second city from a single configuration file**, validated with its own accuracy test rather than Ahmedabad's borrowed numbers. Every data source has global coverage, so any city on Earth is one bounding box away. The benefit reaches furthest to those hit hardest: people who work outdoors and can least afford cooling.

---

## Datasets (Yes, about 175 words)

**What the data is.** A complete environmental picture of the city: vegetation, air, weather, soil and infrastructure, merged onto 146 hexagonal cells so that every stream aligns cell for cell.

**Where it comes from.** NASA's MOD13Q1 satellite vegetation index is the backbone, at 250 m resolution, one reading every 16 days, 42 months of history. The Open-Meteo archive supplies matching monthly air quality. ISRIC SoilGrids provides soil pH and texture for every cell. Sentinel-2 imagery at 10 m locates the bare ground inside each cell. OpenStreetMap and Esri imagery capture current conditions. Every source is free, public, and requires no account or key.

**Why we chose it.** Forecasting decline needs a genuine past to learn from, and MOD13Q1 is the longest free, scientifically validated vegetation record at this resolution. Because the data costs nothing, the data cost of adding a new city is zero, for any municipality on Earth.

**What it solves.** This merged history is what our forecaster learns from, tests itself against, and turns into a ranked, explained, soil aware planting plan. None of it is personal data: no accounts, no logins, nothing about people at all.

---

## Project Stage

**A working prototype performing real AI inference, with a measured figure behind every claim.**

- **Accuracy.** Mean error of ±13.5 AQI points and 0.042 NDVI (about 4% canopy) on held-out months, outperforming a strong statistical baseline.
- **Model selection by competition.** Four approaches, a statistical forecaster, a local language model, a neural network and a random forest, were tested on identical held-out forecasting tasks. The statistical forecaster won and was deployed. The others remain published with their scores, because a system that hides its failed experiments cannot be trusted with a city's planting budget. If a future challenger beats the champion on evidence, the software promotes it automatically.
- **Reliability.** Zero malformed outputs in 30 validation runs of the report engine, with automatic graceful degradation if generation ever fails.
- **Speed.** The language model loads in about 2.5 seconds and writes a cell report in about 4 seconds on a plain laptop CPU. The identical build retargets Intel Core Ultra NPUs through a single setting; those benchmarks are **in progress** on Intel Core i5 and Core Ultra machines.
- **Real world use.** The municipal demonstration and NGO pilot are **in progress**.
- **Honesty by construction.** With 42 months of data, the system itself computes that 23 months is the longest forecast it can genuinely verify, and it refuses to call anything beyond that a prediction; longer projections are stamped UNVALIDATED in the output. The same discipline applies to our own components: when a land cover classifier we built to screen planting sites fell short of its own accuracy gate, the software set it aside automatically and disclosed that in the planting brief.

---

## Intel Technologies (Yes)

Intel technology is the reason this project can keep its three promises: free, local, private.

**Across the complete AI lifecycle.** In *modeling and evaluation*, our challenger models are exported to ONNX and executed by the **OpenVINO runtime**, inside a harness that retests them on held-out months at every run and verifies that OpenVINO's outputs match the training framework's. Training supports **Intel Extension for Scikit-learn**, dispatching accelerated algorithms to Intel oneDAL. In *optimization*, we compress models ourselves with **Intel NNCF**: any open model to INT4, measured at under five minutes on a laptop CPU, producing a 343 MB model from a full precision original. In *deployment*, the **OpenVINO GenAI API** runs our language model entirely on device for every generated report, and a single setting retargets the identical build across **CPU, integrated GPU, and NPU**.

**Hardware, floor to ceiling.** The deployment floor is any Intel CPU a government office already owns. The ceiling is the Intel Core Ultra AI PC now entering public procurement, where the same software runs on the NPU with zero code changes. A per device benchmark suite ships with the project, measuring load time, time to first token, and throughput on every device it finds; measured results from Intel Core i5 and Core Ultra machines are **in progress** and will accompany our demonstration.

**Why Intel.** Our users are government offices with ordinary laptops, not GPU servers. OpenVINO is the single reason that "runs free, runs local, runs private" is a fact rather than a wish.

**Program.** Built through Intel® AI for Youth, applying its Responsible AI framework throughout.

---

## Responsible AI
*(Enable Human Oversight · Enable Transparency and Explainability · Advance Security, Safety and Reliability · Design for Privacy · Protect the Environment)*

**Human oversight.** Green Vision recommends. Planners decide. It never acts on its own.

**Transparency and explainability.** Every recommendation carries a plain language reason, and every statistic is labelled MEASURED, MODELLED or PROJECTED, so an official always knows exactly which numbers they can defend in public.

**Reliability.** Model selection is decided by competition on held-out data, never by preference, and the losing approaches stay published with their scores. Validation limits are enforced by the software itself, including on our own components: a classifier that failed its own audit was automatically set aside and the output says so.

**Fairness and bias.** The model is calibrated on Ahmedabad, so every other region is treated as unvalidated until it is locally retested. Delhi runs its own validation and reports its own accuracy.

**GenAI risk.** The report writer can only restate computed values, in all three output languages. Hallucination is blocked by construction, and formatting failures degrade gracefully instead of crashing.

**Privacy.** No personal data exists anywhere in the system, and all AI inference runs on the user's own machine. Nothing is stored on or sent to an external server.

**Environment.** Model compression means no cloud GPU: a tool for planting trees should not burn a data centre.

**A limitation we disclose.** We noted at the regional round that our traffic light map is difficult for colour blind users, and it still is. Patterns and text labels are planned. We would rather state that than hide it.

---

## SDG Alignment

**Primary: SDG 13, Climate Action.** The innovation is prediction. Existing tools observe green cover loss after it happens; Green Vision forecasts it, so a city can act before the trees are gone. It also scales the way non AI approaches cannot: tree surveys and GIS consultants cost lakhs per city and take months, while free satellite data plus a laptop delivers ranked, explained priorities in seconds, for any city on Earth. Progress is measurable by design, with predicted air quality and canopy change per cell re-audited monthly against new satellite passes, and 25 year canopy and carbon projections for planted cells. The first measurement in the ground is under way: a partner NGO's plantation drive sited with the tool is **in progress**, inside a municipality publicly committed to five million trees this year.

Also aligned: **SDG 11** (Sustainable Cities and Communities), **SDG 15** (Life on Land), **SDG 9** (Industry, Innovation and Infrastructure).

---

## Sources, References and Citations

NASA MOD13Q1 Vegetation Indices, LP DAAC / ORNL DAAC · Open-Meteo Weather and Air Quality APIs · ISRIC SoilGrids v2.0 · ESA WorldCover 2021 · OpenStreetMap contributors · Esri World Imagery · Copernicus Sentinel-2 L2A via AWS Earth Search · World Resources Institute (2019), urban heat island analysis · IQAir World Air Quality Report (2025) · Ahmedabad Municipal Corporation, Mission Five Million Trees, July 2026 · Intel OpenVINO toolkit, OpenVINO GenAI API and NNCF · scikit-learn and Intel Extension for Scikit-learn · Uber H3 · Gitelson et al., VARI vegetation index · Project source: **github.com/pratikb1234/GreenVision**
