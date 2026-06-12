import os, logging, secrets
from flask import Flask, render_template, send_from_directory
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta


# Import your blueprints
from routes.index import main_bp
from routes.inventory import inventory_bp
from routes.card_adder import adder_bp
from routes.trade_binder import trade_bp
from routes.authentication import authentication_bp
from routes.admin import admin_bp
from routes.sets import sets_bp
from routes.card_sorting import sorter_bp
from routes.edh import edh_bp
from routes.collection import collection_bp
from routes.markets import markets_bp

# Import your database manager to load users
from db.db_manager import CardDB, get_db, close_db
from db.user_manager import User
from dotenv import load_dotenv

load_dotenv()

if not os.path.exists("logs"):
    os.makedirs("logs")

app = Flask(__name__)

# if os.path.exists('/var/data'):
#     IMAGE_FOLDER = '/var/data'
# else:
# This 'or' chain ensures IMAGE_FOLDER is NEVER None
IMAGE_FOLDER = os.environ.get('IMAGE_PATH')
app.config['IMAGE_PATH'] = os.getenv('IMAGE_PATH', '/static/images/')

startup_db = CardDB()
try:
    startup_db.create_tables()
    startup_db.commit()
finally:
    startup_db.close()


    
logging.basicConfig(filename="logs/app.log", level=logging.INFO)
logger = logging.getLogger(__name__)
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)

# 1. Configuration
# secret_key is required for session cookies to be encrypted
secret_key = os.environ.get('SECRET_KEY')
if not secret_key:
    secret_key = secrets.token_hex(32)
    logger.warning("SECRET_KEY not set; using ephemeral key for this process.")

app.secret_key = secret_key
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['WTF_CSRF_HEADERS'] = ['X-CSRFToken', 'X-CSRF-Token', 'X-CSFToken']

# 1b. CSRF Protection
csrf = CSRFProtect(app)
app.teardown_appcontext(close_db)

# 2. Flask-Login Setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'authentication.login' # Redirects here if @login_required is triggered

# 3. User Model for Session Management


@login_manager.user_loader
def load_user(user_id):
    """How Flask-Login finds a user in the DB by their ID."""
    manager = get_db()
    user_data = manager.cursor.execute(
        "SELECT user_id, username, role FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()

    if user_data:
        return User(user_data['user_id'], user_data['username'], user_data['role'])

    return None

# 4. Blueprint Registration
app.register_blueprint(main_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(adder_bp)
app.register_blueprint(trade_bp)
app.register_blueprint(authentication_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(sets_bp)
app.register_blueprint(sorter_bp)
app.register_blueprint(edh_bp)
app.register_blueprint(collection_bp)
app.register_blueprint(markets_bp)


# Error handlers
@app.errorhandler(404)
def page_not_found(e):
    logger.error(f"404 Error: {e}")
    return render_template('404.html')

@app.errorhandler(500)
def internal_server_error(e):
    logger.error(f"500 Error: {e}")
    return render_template('404.html')

@app.route('/var/data/<path:filename>')
def serve_card_image(filename):
    return send_from_directory(IMAGE_FOLDER, filename)

if __name__ == '__main__':
    debug_enabled = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes', 'on')
    app.run(debug=debug_enabled)