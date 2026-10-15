# Employee Attendance & Analytics API

FastAPI + MongoDB service implementing an enterprise attendance tracking and analytics system.

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables:**
   Set `MONGO_URI` and `MONGO_DB` via environment variables (or in `.env`). Real environment variables take precedence:
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   All required indexes are created idempotently at application startup (`lifespan`).

## Testing & Verification

- **Seed sample data:**
  ```bash
  python sample_seed.py
  ```

- **Awkward Cases & Concurrency Test Suite:**
  Validates edge cases (zero-log employees, mid-month joiners, tied late minutes, weekend attendance, overnight shifts, missing weekdays, 20 parallel punch-in/out races):
  ```bash
  python test_awkward_and_concurrency.py
  ```

- **100,000-Record Seeding & Explain Verifier:**
  Seeds 100,000 records and verifies `IXSCAN` with zero `COLLSCAN` across all 5 explained endpoints:
  ```bash
  python verify_100k_explain.py
  ```

## Documentation
- `REVIEW.md`: Details the 10 defects identified in the starter code and their fixes.
- `DECISIONS.md`: Answers key architectural questions on indexing, concurrency races, ties, headcount, and 100x scaling.
