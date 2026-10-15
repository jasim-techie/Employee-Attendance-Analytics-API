# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service for managing employee attendance, punch-in/out lifecycles, manual corrections (regularizations), and aggregated HR analytics.

Built for the HROne Software Engineer Trainee technical assignment, strictly adhering to the OpenAPI 3.0 specification (`openapi.yaml`), business rules R1 through R10, and stored document schemas (`DATA_MODEL.md`).

---

## 1. What the Project Does

- **Employee Management:** Registers employees with immutable `emp_code`, email, department, working shifts, and joining date.
- **Punch-In / Punch-Out (R1 - R5):** 
  - Computes IST attendance dates (including overnight shifts crossing midnight).
  - Calculates grace-period late minutes (`late_minutes`).
  - Computes work hours using exact half-up rounding (`ROUND_HALF_UP`).
  - Identifies half-days (< 4.50 hours) and overtime (>= 30 minutes).
  - Guarantees concurrency safety (preventing duplicate simultaneous punch-ins or punch-outs).
- **Attendance Corrections & Audit Trail:** Supports manual regularization (`PATCH /attendance/{emp_code}/{date}`) with field-level before/after diff audit history.
- **MongoDB Aggregation Analytics:** 
  - Single employee monthly summary (`/analytics/employees/{emp_code}/monthly`).
  - Department summary across collections with headcount including zero-log employees (`/analytics/departments/summary`).
  - Late-comers leaderboard with standard competition ranking ties (1, 2, 2, 4) (`/analytics/leaderboard/late`).
  - Department trend series with database gap-filling and 7-day moving averages (`/analytics/departments/{department}/trend`).
- **Database Query Explain (`/admin/explain/{endpoint}`):** Generates `executionStats` query plans verifying index usage (`IXSCAN`) and absence of collection scans (`COLLSCAN`).

---

## 2. Prerequisites

- **Python:** 3.11+
- **MongoDB:** 6.0+ (Tested against MongoDB 7.0/9.0 and compatible with MongoDB Atlas M0)
- **Git**

---

## 3. Environment Variables

The application reads configuration from environment variables (or a local `.env` file for local development):

| Variable | Description | Default Value |
|---|---|---|
| `MONGO_URI` | MongoDB connection connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Target database name | `attendance_db` |

Example configuration:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

---

## 4. How to Install Dependencies

```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# On Linux/macOS:
source .venv/bin/activate
# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1

# Install runtime dependencies
pip install -r requirements.txt

# (Optional) Install testing tools
pip install pytest httpx
```

---

## 5. How to Start MongoDB

### Local MongoDB Service / Installation
If MongoDB is installed locally:
```bash
# Windows
net start MongoDB
# Linux (systemd)
sudo systemctl start mongod
```

### Docker
```bash
docker run -d -p 27017:27017 --name mongodb mongo:7
```

---

## 6. How to Start the FastAPI Application

Seed sample data into your database (optional):
```bash
python sample_seed.py
```

Run the API server with Uvicorn:
```bash
uvicorn app.main:app --port 8000
```
Or with auto-reload for development:
```bash
uvicorn app.main:app --port 8000 --reload
```

---

## 7. Where to Access API Documentation

Once the application is running, interactive API documentation is available at:

- **Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **Raw OpenAPI Schema:** [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

---

## 8. How to Run Tests

Run the automated test suite with pytest:

```bash
pytest -v
```

All 13 automated tests validate core business rules, edge cases (overnight shifts, competition ranking ties, mid-month joiners, weekend logs), concurrency protection, and database query plan index usage.

---

## 9. Requirements Implementation Status

| Feature / Requirement | Status | Notes |
|---|---|---|
| `GET /health` | Completed | Returns 200 on DB ping, 503 if unreachable |
| `POST /employees` | Completed | Enforces unique `emp_code`, validated formats |
| `GET /employees` | Completed | Paginated, filtered by department, sorted by `emp_code` asc |
| `POST /attendance/punch-in` | Completed | Concurrency-safe unique constraint, R1 IST dates, R2 late minutes |
| `POST /attendance/punch-out` | Completed | Concurrency-safe atomic update, R3 overtime, R4 half-up work hours, R5 half-day |
| `GET /attendance` | Completed | Indexed pagination, sorted by `date` desc, `emp_code` asc |
| `PATCH /attendance/{emp_code}/{date}` | Completed | Recomputes derived fields, appends audit trail `history`, optimistic locking |
| `GET /analytics/employees/{emp_code}/monthly` | Completed | Mon-Fri working days, mid-month joiner handling, present-days calculation |
| `GET /analytics/departments/summary` | Completed | Cross-collection aggregation, headcount includes zero-log employees |
| `GET /analytics/leaderboard/late` | Completed | Competition ranking (1, 2, 2, 4) with ties at limit cutoff |
| `GET /analytics/departments/{department}/trend` | Completed | Full gap-filling with `$densify`, 7-day moving average |
| `GET /admin/explain/{endpoint}` | Completed | Uses `IXSCAN` backed by compound indexes, zero `COLLSCAN` |
| `REVIEW.md` | Completed | Documents 11 defects found and resolved |
| `DECISIONS.md` | Completed | Explains key architectural and algorithmic decisions (150-200 words) |

---

## 10. Known Considerations & Limitations

- **MongoDB Deployment Support for Transactions:** Manual regularizations use document-level optimistic locking on array length (`history.{len}: {"$exists": False}`) for atomic updates. In standalone MongoDB instances without replica sets, this guarantees single-document consistency without requiring multi-document distributed transactions.
- **Trend Moving Average Range Boundaries:** As specified in the contract, 7-day moving average windows consider only rows within the requested `from`-`to` range, meaning the first 6 days compute their mean across a shorter window.
