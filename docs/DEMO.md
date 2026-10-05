# Two-minute demonstration script

1. Show `config/seeds.yaml`: these are official hubs and search queries, with no hard-coded scholarship facts. Run `python -m scholarship_intel run --max-pages 30` (or show the completed run in the dashboard's **Crawl runs** tab). Point out a discovered official URL, its source classification, extraction, confidence, and database write in the terminal output.
2. Run `python -m scholarship_intel serve` and open <http://127.0.0.1:8000>. Show the dashboard totals and search for one scholarship. Open it; show **Official source**, **Why this score?**, and a field's **Trace evidence** button.
3. Run the crawler again with `python -m scholarship_intel run --reverify-only --llm groq` to demonstrate repeat operation. Open **Crawl runs** to show the second run and unchanged or changed pages.
4. Run `python -m scholarship_intel demo-changes` to create a separate replay database. Start it with `ATLAS_DB=data/atlas_demo.db python -m scholarship_intel serve --port 8001`, then open <http://127.0.0.1:8001>. Show the prominent **DEMO DATABASE** notice and **SIMULATED** badges. Open **Changes** and one scholarship's history to show old value, new value, detection time, source, and evidence. Show expired/removed examples. Explain that these edits were injected into a copy because live pages rarely change during a short recording.
5. Show `data/atlas.db` and `data/sample/` in the repository. These contain the real crawl's inspectable output; the demo database is separate and ignored by Git.

For a video, record the terminal and browser while following these steps. Never show the `.env` file or its values in the recording.
