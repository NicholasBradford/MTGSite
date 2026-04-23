import os, logging
from flask import Flask, render_template, send_from_directory
from flask_login import LoginManager
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

# Import your database manager to load users
from db.db_manager import CardDB
from db.user_manager import User
from dotenv import load_dotenv

if not os.path.exists("logs"):
    os.makedirs("logs")

app = Flask(__name__)

# if os.path.exists('/var/data'):
#     IMAGE_FOLDER = '/var/data'
# else:
# This 'or' chain ensures IMAGE_FOLDER is NEVER None
IMAGE_FOLDER = os.environ.get('IMAGE_PATH')

start_app = CardDB()
start_app.create_tables()
start_app.commit()
start_app.close()


    
logging.basicConfig(filename="logs/app.log", level=logging.INFO)
logger = logging.getLogger(__name__)
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)

# 1. Configuration
# secret_key is required for session cookies to be encrypted
app.secret_key = os.environ.get('SECRET_KEY') or 'dev-key-placeholder'

# 2. Flask-Login Setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'authentication.login' # Redirects here if @login_required is triggered

# 3. User Model for Session Management


@login_manager.user_loader
def load_user(user_id):
    """How Flask-Login finds a user in the DB by their ID."""
    manager = CardDB()
    
    user_data = manager.cursor.execute(
        "SELECT user_id, username, role FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    manager.close()
    
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
    app.run(debug=False)