# Helsinki Home Dashboard

Map of Helsinki postal-code areas and buildings with dwelling prices, rents, census figures, schools, high schools, transit and noise.

Open it: https://esamuikkunen.github.io/helsinki-dashboard/

## Data

The page loads saved copies of its data from `data/` so it opens quickly. A GitHub Actions job (`.github/workflows/update-data.yml`, script `scripts/fetch_data.py`) refreshes them on the 3rd of each month; run it any time from **Actions → Update dashboard data → Run workflow**. Buildings still load live from the City of Helsinki when you zoom in. If a saved file is missing, the page falls back to the live source.

Sources: Statistics Finland (Paavo, dwelling prices, rents; CC BY 4.0), City of Helsinki building register and noise survey (CC BY 4.0), © OpenStreetMap contributors (ODbL), väestökello.fi, s2koulut.xyz, kouluhaku.fi, yhteishaku.app. Building values are rough estimates, not valuations.
