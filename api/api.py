import time
import uuid

import redis
from flask import Flask, request, jsonify, make_response, render_template
from flask_limiter import Limiter
import logging
from flask_cors import CORS
from dotenv import load_dotenv
import os
from flask_limiter.util import get_remote_address

from functools import wraps
import secrets

load_dotenv()

ADMIN_KEY = os.getenv("ADMIN_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)
SESSION_COOKIE_NAME = "sid"
SESSION_TTL = 3600

class RedisDatabase:
    def __init__(self):
        try:
            self.client = redis.Redis(
                host=os.getenv("REDIS_HOST", "127.0.0.1"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                password=os.getenv("REDIS_PASSWORD"),
                decode_responses=True,
                socket_timeout=5
            )
            self.client.ping()  # Test the client
            logger.info(f"Connected to Redis database")
        except redis.AuthenticationError:
            logger.critical("Redis authentication failed. Check your password.")
            raise
        except redis.ConnectionError as e:
            logger.critical(f"Failed to connect to Redis: {e}")
            raise

    def get(self, key):
        return self.client.get(key)

    def set(self, key, value, ex=None, nx=False):
        return self.client.set(key, value, ex=ex, nx=nx)

    def uptime(self):
        info = self.client.info()
        return info.get('uptime_in_seconds', 0)

    def increment(self, key):
        return self.client.incr(key)

    def info(self):
        return self.client.info()

    def exists(self, key):
        return self.client.exists(key)

    def hset(self, key, mapping):
        return self.client.hset(key, mapping=mapping)


    def connect(self):
        # Logic to connect to the database using self.db_url
        pass

    def disconnect(self):
        # Logic to disconnect from the database
        pass

    def execute_query(self, query):
        # Logic to execute a query on the database
        pass


app = Flask(__name__, template_folder='../templates')
CORS(app)  # Enable CORS for all routes
redis_db = RedisDatabase()  # Initialize Redis database connection
ratelimitter = Limiter(
    key_func=get_remote_address, 
    default_limits=["100 per minute"], 
    storage_uri=f"redis://:{os.getenv('REDIS_PASSWORD')}@{os.getenv('REDIS_HOST', '127.0.0.1')}:{os.getenv('REDIS_PORT', 6379)}",
    app=app)

RESERVED_KEYS = {"demo-site"}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health_check():
    try:
        redis_db.client.ping()  # Check if Redis is reachable
        return {"status": "healthy"}, 200
    except redis.ConnectionError:
        return {"status": "unhealthy"}, 500

@app.route('/data/<key>', methods=['GET'])
@ratelimitter.limit("100 per minute")
def get_data(key):
    try:
        value = redis_db.get(key)
        if value is None:
            return {"error": "Key not found"}, 404
        return {"key": key, "value": value}, 200
    except Exception as e:
        logger.error(f"Error retrieving data for key {key}: {e}")
        return {"error": "Internal server error"}, 500

def validate_data(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        return True
    specials = ['/', '\\', '\0', ' ', '\n', '\r', '\t']
    return any(char in value for char in specials)

def require_registered(key):
    """True if this site key has been provisioned via /admin/register."""
    return bool(redis_db.exists(f"auth:sitekey:{key}"))

def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ADMIN_KEY:
            logger.critical("ADMIN_KEY is not set — refusing admin request.")
            return {"error": "Server misconfigured"}, 500
        supplied = request.headers.get('X-Admin-Key')
        if not supplied or not secrets.compare_digest(supplied, ADMIN_KEY):
            return {"error": "Unauthorized"}, 401
        return f(*args, **kwargs)
    return wrapper

@app.route('/admin/register', methods=['POST'])
@require_admin
def register_site():
    try:
        body = request.json or {}
        name = body.get('name')
        site_key = body.get('site_key')
 
        if not name or not site_key:
            return {"error": "name and site_key are required"}, 400
        if validate_data(site_key) or validate_data(name):
            return {"error": "Invalid name or site_key format"}, 400
        if redis_db.exists(f"auth:sitekey:{site_key}"):
            return {"error": "site_key already registered"}, 409
 
        api_key = secrets.token_hex(24)
        redis_db.hset(f"auth:apikey:{api_key}", {
            "name": name,
            "site_key": site_key,
            "created_at": str(int(time.time())),
        })
        redis_db.client.set(f"auth:sitekey:{site_key}", api_key)
 
        logger.info(f"Registered new site_key '{site_key}' for '{name}'")
        # api_key is shown exactly once — the caller (you) needs to save and hand it off.
        return {"site_key": site_key, "api_key": api_key}, 201
    except Exception as e:
        logger.error(f"Error registering site: {e}")
        return {"error": "Internal server error"}, 500

@app.route('/data/<key>', methods=['POST'])
@ratelimitter.limit("100 per minute")
def set_data(key):
    try:
        raise ValueError("YOU CAN'T SET DATA")
        value = request.json.get('value')
        if value is None:
            return {"error": "Value is required"}, 400
        if validate_data(value):
            return {"error": "Invalid data format"}, 400
        redis_db.set(key, value)
        return {"message": "Data set successfully"}, 200
    except Exception as e:
        logger.error(f"Error setting data for key {key}: {e}")
        return {"error": "Internal server error"}, 500

@app.route('/hit/<key>', methods=['GET'])
@ratelimitter.limit("100 per minute")
def hit_counter(key):
    try:
        if validate_data(key):
            return {"error": "Invalid data format"}, 400

        if not require_registered(key):
            return {"error": "Unknown site key"}, 404
        totalKey = f"{key}:visits:total"
        sessionId = request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get('Session-Id')

        # logger.info(f"Session ID from cookie or header: {sessionId}")

        if sessionId is None:
            sessionId = str(uuid.uuid4())
        sessionKey = f"{key}:sessions:{sessionId}"

        is_new = redis_db.set(sessionKey, 1, ex=3600, nx=True)
        if is_new:
            redis_db.increment(totalKey)

        current_count = redis_db.get(totalKey) or 0

        # logger.info(f"Hit count for key '{key}': {current_count}, Session ID: {sessionId}")

        resp = make_response({"visits": current_count}, 200)

        resp.set_cookie(
            SESSION_COOKIE_NAME,
            sessionId,
            max_age=SESSION_TTL,
            httponly=True,      
            secure=not app.debug,
            samesite="Lax",
        )
        return resp
    except Exception as e:
        logger.error(f"Error updating hit count: {e}")
        return {"error": "Internal server error"}, 500

@app.route('/status', methods=['GET'])
def status():
    info = redis_db.info()
    safe_info = {
        "redis_version": info.get("redis_version"),
        "uptime_in_seconds": info.get("uptime_in_seconds"),
        "connected_clients": info.get("connected_clients"),
        "used_memory_human": info.get("used_memory_human"),
        "used_memory_peak_human": info.get("used_memory_peak_human"),
        "total_connections_received": info.get("total_connections_received"),
        "total_commands_processed": info.get("total_commands_processed"),
        "instantaneous_ops_per_sec": info.get("instantaneous_ops_per_sec"),
        "keyspace_hits": info.get("keyspace_hits"),
        "keyspace_misses": info.get("keyspace_misses"),
        "role": info.get("role"),
    }
    return jsonify({"status": "running", "redis_info": safe_info}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)