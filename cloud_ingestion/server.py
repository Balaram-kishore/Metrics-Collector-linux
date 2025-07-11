from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from typing import Dict, Any, List, Optional, Union
import uvicorn
import sqlite3
import json
import logging
import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
import threading
from contextlib import contextmanager

# Import InfluxDB adapter
try:
    from influxdb_adapter import InfluxDBAdapter
    INFLUXDB_AVAILABLE = True
except ImportError:
    INFLUXDB_AVAILABLE = False
    InfluxDBAdapter = None

# Configure structured JSON logging (assignment requirement)
class JSONFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging."""

    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "service": "metrics-ingestion"
        }

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add extra fields if present
        for key, value in record.__dict__.items():
            if key not in ['name', 'msg', 'args', 'levelname', 'levelno', 'pathname',
                          'filename', 'module', 'lineno', 'funcName', 'created',
                          'msecs', 'relativeCreated', 'thread', 'threadName',
                          'processName', 'process', 'getMessage', 'exc_info', 'exc_text', 'stack_info']:
                log_entry[key] = value

        return json.dumps(log_entry)

# Setup structured logging
def setup_logging():
    """Setup structured JSON logging for the ingestion service."""
    # Create logs directory
    log_dir = Path('logs')
    log_dir.mkdir(exist_ok=True)

    # Configure handlers with JSON formatting
    file_handler = logging.FileHandler(log_dir / 'metrics_ingestion.log')
    file_handler.setFormatter(JSONFormatter())

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JSONFormatter())

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

# Initialize logging
setup_logging()
logger = logging.getLogger(__name__)
logger.info("Metrics ingestion service starting", extra={
    "service": "metrics-ingestion",
    "version": "1.0.0"
})

app = FastAPI(
    title="Metrics Ingestion Service",
    description="Cloud-based metrics ingestion and storage service",
    version="1.0.0"
)

# Add CORS middleware for dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CPUMetrics(BaseModel):
    percent: float = Field(..., ge=0, le=100)
    count: int = Field(..., gt=0)
    count_logical: int = Field(..., gt=0)
    load_avg: Optional[List[float]] = None

class MemoryMetrics(BaseModel):
    total: int = Field(..., gt=0)
    available: int = Field(..., ge=0)
    percent: float = Field(..., ge=0, le=100)
    used: int = Field(..., ge=0)
    free: int = Field(..., ge=0)
    buffers: int = Field(default=0, ge=0)
    cached: int = Field(default=0, ge=0)

class SwapMetrics(BaseModel):
    total: int = Field(..., ge=0)
    used: int = Field(..., ge=0)
    free: int = Field(..., ge=0)
    percent: float = Field(..., ge=0, le=100)

class DiskMetrics(BaseModel):
    device: str
    mountpoint: str
    fstype: str
    total: int = Field(..., gt=0)
    used: int = Field(..., ge=0)
    free: int = Field(..., ge=0)
    percent: float = Field(..., ge=0, le=100)

class NetworkMetrics(BaseModel):
    bytes_sent: int = Field(..., ge=0)
    bytes_recv: int = Field(..., ge=0)
    packets_sent: int = Field(..., ge=0)
    packets_recv: int = Field(..., ge=0)
    errin: int = Field(default=0, ge=0)
    errout: int = Field(default=0, ge=0)
    dropin: int = Field(default=0, ge=0)
    dropout: int = Field(default=0, ge=0)

class ProcessMetrics(BaseModel):
    pid: int
    name: str
    cpu_percent: float = Field(..., ge=0)
    memory_percent: float = Field(..., ge=0, le=100)

class SystemMetrics(BaseModel):
    timestamp: str
    hostname: str
    cpu: CPUMetrics
    memory: MemoryMetrics
    swap: SwapMetrics
    disk: List[DiskMetrics]
    network: Optional[NetworkMetrics] = None
    top_processes: Optional[List[ProcessMetrics]] = None
    error: Optional[str] = None

    @validator('timestamp')
    def validate_timestamp(cls, v):
        try:
            datetime.fromisoformat(v.replace('Z', '+00:00'))
            return v
        except ValueError:
            raise ValueError('Invalid timestamp format')

class MetricsPayload(BaseModel):
    hostname: str = Field(..., min_length=1, max_length=255)
    metrics: SystemMetrics

class MetricsDatabase:
    def __init__(self, db_path: str = "metrics.db"):
        self.db_path = Path(db_path)
        self.lock = threading.Lock()
        self._init_database()

    def _init_database(self):
        """Initialize the SQLite database with required tables."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    cpu_percent REAL,
                    memory_percent REAL,
                    memory_total INTEGER,
                    memory_used INTEGER,
                    swap_percent REAL,
                    disk_data TEXT,  -- JSON string
                    network_data TEXT,  -- JSON string
                    raw_data TEXT,  -- Complete JSON payload
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_timestamp ON metrics(timestamp);
                CREATE INDEX IF NOT EXISTS idx_hostname ON metrics(hostname);
                CREATE INDEX IF NOT EXISTS idx_created_at ON metrics(created_at);

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    value REAL,
                    threshold REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_hostname ON alerts(hostname);
            """)

    @contextmanager
    def _get_connection(self):
        """Get a database connection with proper error handling."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            raise e
        finally:
            if conn:
                conn.close()

    def store_metrics(self, payload: MetricsPayload) -> bool:
        """Store metrics in the database."""
        try:
            with self.lock:
                with self._get_connection() as conn:
                    metrics = payload.metrics

                    # Extract key metrics for easy querying
                    disk_data = json.dumps([disk.dict() for disk in metrics.disk])
                    network_data = json.dumps(metrics.network.dict()) if metrics.network else None
                    raw_data = json.dumps(payload.dict())

                    conn.execute("""
                        INSERT INTO metrics (
                            timestamp, hostname, cpu_percent, memory_percent,
                            memory_total, memory_used, swap_percent,
                            disk_data, network_data, raw_data
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        metrics.timestamp,
                        payload.hostname,
                        metrics.cpu.percent,
                        metrics.memory.percent,
                        metrics.memory.total,
                        metrics.memory.used,
                        metrics.swap.percent,
                        disk_data,
                        network_data,
                        raw_data
                    ))

                    conn.commit()
                    return True

        except Exception as e:
            logger.error(f"Error storing metrics: {e}")
            return False

    def get_recent_metrics(self, hostname: Optional[str] = None, hours: int = 24) -> List[Dict]:
        """Retrieve recent metrics from the database."""
        try:
            with self._get_connection() as conn:
                since_time = datetime.utcnow() - timedelta(hours=hours)

                if hostname:
                    cursor = conn.execute("""
                        SELECT * FROM metrics
                        WHERE hostname = ? AND datetime(created_at) > datetime(?)
                        ORDER BY created_at DESC
                        LIMIT 1000
                    """, (hostname, since_time.isoformat()))
                else:
                    cursor = conn.execute("""
                        SELECT * FROM metrics
                        WHERE datetime(created_at) > datetime(?)
                        ORDER BY created_at DESC
                        LIMIT 1000
                    """, (since_time.isoformat(),))

                return [dict(row) for row in cursor.fetchall()]

        except Exception as e:
            logger.error(f"Error retrieving metrics: {e}")
            return []

    def get_summary_stats(self, hostname: Optional[str] = None, hours: int = 24) -> Dict:
        """Get summary statistics for metrics."""
        try:
            with self._get_connection() as conn:
                since_time = datetime.utcnow() - timedelta(hours=hours)

                base_query = """
                    SELECT
                        COUNT(*) as total_records,
                        AVG(cpu_percent) as avg_cpu,
                        MAX(cpu_percent) as max_cpu,
                        AVG(memory_percent) as avg_memory,
                        MAX(memory_percent) as max_memory,
                        AVG(swap_percent) as avg_swap,
                        MAX(swap_percent) as max_swap
                    FROM metrics
                    WHERE datetime(created_at) > datetime(?)
                """

                if hostname:
                    cursor = conn.execute(base_query + " AND hostname = ?", (since_time.isoformat(), hostname))
                else:
                    cursor = conn.execute(base_query, (since_time.isoformat(),))

                result = cursor.fetchone()
                return dict(result) if result else {}

        except Exception as e:
            logger.error(f"Error getting summary stats: {e}")
            return {}

    def cleanup_old_data(self, days_to_keep: int = 30):
        """Remove old metrics data to prevent database growth."""
        try:
            with self.lock:
                with self._get_connection() as conn:
                    cutoff_date = datetime.utcnow() - timedelta(days=days_to_keep)

                    cursor = conn.execute("""
                        DELETE FROM metrics
                        WHERE datetime(created_at) < datetime(?)
                    """, (cutoff_date.isoformat(),))

                    deleted_count = cursor.rowcount
                    conn.commit()

                    if deleted_count > 0:
                        logger.info(f"Cleaned up {deleted_count} old metric records")

                    return deleted_count

        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
            return 0

class DatabaseFactory:
    """Factory for creating database instances based on configuration."""

    @staticmethod
    def create_database(config: Dict[str, Any] = None) -> Union[MetricsDatabase, InfluxDBAdapter]:
        """Create database instance based on configuration."""
        if config is None:
            config = {}

        db_type = config.get('type', 'sqlite').lower()

        if db_type == 'influxdb' and INFLUXDB_AVAILABLE:
            logger.info("Using InfluxDB for time-series storage (assignment requirement)")
            return InfluxDBAdapter(config.get('influxdb', {}))
        else:
            if db_type == 'influxdb' and not INFLUXDB_AVAILABLE:
                logger.warning("InfluxDB requested but not available, falling back to SQLite")
            logger.info("Using SQLite for metrics storage")
            return MetricsDatabase(config.get('sqlite', {}).get('path', 'metrics.db'))

# Load database configuration
def load_db_config() -> Dict[str, Any]:
    """Load database configuration from environment or config file."""
    config = {
        'type': os.getenv('DB_TYPE', 'sqlite'),
        'sqlite': {
            'path': os.getenv('SQLITE_PATH', 'metrics.db')
        },
        'influxdb': {
            'url': os.getenv('INFLUXDB_URL', 'http://localhost:8086'),
            'token': os.getenv('INFLUXDB_TOKEN'),
            'org': os.getenv('INFLUXDB_ORG', 'metrics-org'),
            'bucket': os.getenv('INFLUXDB_BUCKET', 'metrics'),
            'timeout': int(os.getenv('INFLUXDB_TIMEOUT', '10000'))
        }
    }

    # Try to load from config file if it exists
    config_file = Path('db_config.json')
    if config_file.exists():
        try:
            with open(config_file) as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            logger.warning(f"Failed to load database config file: {e}")

    return config

# Global database instance
db_config = load_db_config()
db = DatabaseFactory.create_database(db_config)

# Dependency to get database instance
def get_database():
    return db

@app.post("/ingest")
async def ingest_metrics(
    payload: MetricsPayload,
    background_tasks: BackgroundTasks,
    db = Depends(get_database)
):
    """Ingest metrics from collectors (assignment requirement: HTTP ingestion endpoint)."""
    try:
        # Log incoming metrics with structured logging
        logger.info("Received metrics payload", extra={
            "hostname": payload.hostname,
            "timestamp": payload.metrics.timestamp,
            "has_cpu_data": 'cpu' in payload.metrics.dict(),
            "has_memory_data": 'memory' in payload.metrics.dict(),
            "has_disk_data": 'disk' in payload.metrics.dict(),
            "payload_size": len(json.dumps(payload.dict()))
        })

        # Store metrics based on database type
        if isinstance(db, InfluxDBAdapter):
            success = db.store_metrics(payload.hostname, payload.metrics.dict())
        else:
            success = db.store_metrics(payload)

        if success:
            logger.info("Metrics stored successfully", extra={
                "hostname": payload.hostname,
                "database_type": type(db).__name__
            })
            return {
                "status": "success",
                "message": "Metrics stored successfully",
                "hostname": payload.hostname,
                "timestamp": payload.metrics.timestamp
            }
        else:
            logger.error("Failed to store metrics", extra={
                "hostname": payload.hostname,
                "database_type": type(db).__name__
            })
            raise HTTPException(status_code=500, detail="Failed to store metrics")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing metrics", extra={
            "hostname": payload.hostname,
            "error_type": type(e).__name__,
            "error": str(e)
        }, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/metrics")
async def get_metrics(
    hostname: Optional[str] = None,
    hours: int = 24,
    db = Depends(get_database)
):
    """Retrieve recent metrics from time-series store."""
    try:
        logger.info("Retrieving metrics", extra={
            "hostname": hostname,
            "hours": hours,
            "database_type": type(db).__name__
        })

        metrics = db.get_recent_metrics(hostname=hostname, hours=hours)

        return {
            "status": "success",
            "count": len(metrics),
            "metrics": metrics,
            "database_type": type(db).__name__,
            "query_params": {
                "hostname": hostname,
                "hours": hours
            }
        }
    except Exception as e:
        logger.error("Error retrieving metrics", extra={
            "hostname": hostname,
            "hours": hours,
            "error": str(e)
        }, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve metrics")

@app.get("/metrics/summary")
async def get_metrics_summary(
    hostname: Optional[str] = None,
    hours: int = 24,
    db = Depends(get_database)
):
    """Get summary statistics for metrics."""
    try:
        logger.info("Retrieving metrics summary", extra={
            "hostname": hostname,
            "hours": hours,
            "database_type": type(db).__name__
        })

        stats = db.get_summary_stats(hostname=hostname, hours=hours)

        return {
            "status": "success",
            "summary": stats,
            "period_hours": hours,
            "hostname": hostname,
            "database_type": type(db).__name__
        }
    except Exception as e:
        logger.error("Error getting summary", extra={
            "hostname": hostname,
            "hours": hours,
            "error": str(e)
        }, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get summary")

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "metrics-ingestion"
    }

@app.post("/cleanup")
async def cleanup_data(days_to_keep: int = 30, db: MetricsDatabase = Depends(get_database)):
    """Manually trigger data cleanup."""
    try:
        deleted_count = db.cleanup_old_data(days_to_keep)
        return {
            "status": "success",
            "deleted_records": deleted_count,
            "days_kept": days_to_keep
        }
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        raise HTTPException(status_code=500, detail="Cleanup failed")

# Background task for periodic cleanup
async def periodic_cleanup():
    """Periodic cleanup task."""
    while True:
        try:
            await asyncio.sleep(24 * 60 * 60)  # Run daily
            db.cleanup_old_data(days_to_keep=30)
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}")

@app.on_event("startup")
async def startup_event():
    """Initialize background tasks on startup."""
    logger.info("Starting Metrics Ingestion Service")
    # Start periodic cleanup task
    asyncio.create_task(periodic_cleanup())

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("Shutting down Metrics Ingestion Service")

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Metrics Ingestion Service')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8000, help='Port to bind to')
    parser.add_argument('--reload', action='store_true', help='Enable auto-reload for development')

    args = parser.parse_args()

    uvicorn.run(
        "server:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload
    )