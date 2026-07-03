import os
from urllib.parse import urlparse, urljoin

from flask import request, Blueprint, redirect, url_for, render_template, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from db.db_manager import get_db
from db.user_manager import User

authentication_bp = Blueprint('authentication', __name__)


def is_safe_url(target):
    """
    Prevent open redirects by only allowing local redirects.
    """
    if not target:
        return False

    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))

    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


def get_safe_next_url(default_endpoint="main.index"):
    """
    Pulls ?next=/some/page safely.
    Falls back to the given endpoint.
    """
    next_url = request.args.get("next") or request.form.get("next")

    if next_url and is_safe_url(next_url):
        return next_url

    return url_for(default_endpoint)


def is_credentialed_path(path):
    """
    Paths that should not remain visible after logout.
    Add to this list as your app grows.
    """
    credentialed_prefixes = (
        "/inventory",
        "/add",
        "/admin",
        "/market",
        "/logout",
        "/register",
    )

    return path.startswith(credentialed_prefixes)


@authentication_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(get_safe_next_url())

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        manager = get_db()
        user = manager.cursor.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,)
        ).fetchone()
        manager.close()

        if user and check_password_hash(user['password_hash'], password):
            user_obj = User(user['user_id'], user['username'], user['role'])
            remember_me = True if request.form.get('remember') else False

            login_user(user_obj, remember=remember_me)

            return redirect(get_safe_next_url())

        flash('Invalid username or password')

    next_url = request.args.get("next")

    if not next_url:
        referrer = request.referrer

        if referrer and is_safe_url(referrer):
            parsed_referrer = urlparse(referrer)

            # Do not bounce back to login/logout/register pages
            if parsed_referrer.path not in ("/login", "/logout", "/register"):
                next_url = referrer

    return render_template(
        'login.html',
        next=next_url or ""
    )


@authentication_bp.route('/logout')
@login_required
def logout():
    next_url = request.referrer or url_for('main.index')

    logout_user()

    if is_safe_url(next_url):
        parsed_next = urlparse(next_url)
        next_path = parsed_next.path

        if not is_credentialed_path(next_path):
            return redirect(next_url)

    return redirect(url_for('main.index'))


@authentication_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        requested_role = request.form.get('role', 'user')
        admin_key = request.form.get('admin_key')
        hashed_pw = generate_password_hash(password)

        manager = get_db()

        try:
            user_count = manager.cursor.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            local_app_mode = os.environ.get("LOCAL_APP_MODE") == "1"

            if local_app_mode and user_count == 0:
                # Packaged local app: first account owns the local install.
                role = "admin"
            else:
                role = requested_role

                if role == "admin":
                    secret_admin_phrase = os.environ.get('ADMIN_REGISTRATION_KEY')

                    if not secret_admin_phrase or admin_key != secret_admin_phrase:
                        flash('Cannot create ADMIN account without site owner authorization')
                        return redirect(url_for('authentication.login'))

            manager.cursor.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, hashed_pw, role)
            )
            manager.commit()

            flash('Account created! Please log in.')
            return redirect(url_for('authentication.login'))

        except Exception as e:
            print(f"Registration Error: {e}")
            flash('An error occurred during registration.')

        finally:
            manager.close()

    return render_template('register.html')