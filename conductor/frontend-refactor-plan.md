# Frontend Refactor Plan

## Objective
Refactor the existing Streamlit application (`app.py`) into a modern, independent architecture consisting of a Python FastAPI backend and a Vite-built frontend using HTML, Tailwind CSS, and Flowbite.

## Key Files & Context
- `app.py`: Current Streamlit UI and data aggregation logic (to be replaced).
- `pyproject.toml` / `requirements.txt`: Python dependencies (need to add `fastapi`, `uvicorn`, remove `streamlit`).
- `src/api/`: New directory for the FastAPI application and route handlers.
- `web/`: New directory for the Vite frontend project.

## Implementation Steps

### Phase 1: Backend API Setup (FastAPI)
1. **Dependencies**: Add `fastapi` and `uvicorn` to `pyproject.toml` and `requirements.txt`. Remove `streamlit`.
2. **API App Creation**: Create `src/api/main.py` to initialize the FastAPI application and configure CORS.
3. **Route Migration**: Port the data aggregation logic from `app.py` into RESTful endpoints:
   - `GET /api/overview`: Market direction, sentiment, macro, fund flow.
   - `GET /api/market/indices`: Major index data.
   - `GET /api/market/etfs`: ETF ranking data.
   - `GET /api/market/sectors`: Sector heatmaps.
   - `GET /api/portfolio`: Holdings with calculated scores and PnL.
   - `GET /api/risk`: Risk alerts and anomaly detection.
   - `POST /api/trigger`: Manual analysis trigger.
4. **Backend CLI**: Update `main.py` or add a command to run the API server (`uvicorn src.api.main:app`).

### Phase 2: Frontend Setup (Vite + Flowbite)
1. **Project Initialization**: Initialize a new Vite vanilla project in the `web/` directory.
2. **Dependencies**: Install `tailwindcss`, `flowbite`, `postcss`, `autoprefixer`, and `plotly.js-dist-min` via npm.
3. **Configuration**: Configure `tailwind.config.js` to include Flowbite plugins and paths. Set up the Vite dev server proxy to route `/api` calls to the FastAPI backend.
4. **Base Layout**: Implement the main dashboard shell (Sidebar, Header, Main Content Area) using Flowbite components.

### Phase 3: UI Implementation
1. **Routing/Navigation**: Implement basic client-side navigation or tab switching to mimic the Streamlit sidebar.
2. **Pages**:
   - **Overview**: Build metric cards, sentiment gauge charts (using Plotly.js), and market temperature indicators.
   - **ETF Rankings**: Implement data tables with sorting and a top 10 bar chart.
   - **Sector Heatmap**: Implement sector performance bar charts and data tables.
   - **Portfolio**: Build PnL summary cards and individual holding score cards.
   - **Risk Ladder**: Implement risk alert panels categorized by severity.
3. **API Integration**: Connect each page to its respective FastAPI endpoint to fetch and render dynamic data.

### Phase 4: Cleanup
1. Delete `app.py`.
2. Ensure the `uv.lock` is updated.

## Verification & Testing
- Start the FastAPI backend and verify endpoints return valid JSON.
- Start the Vite dev server and verify the UI loads without errors.
- Ensure all charts render correctly using Plotly.js.
- Verify the manual trigger process works end-to-end via the API.
