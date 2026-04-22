import os
from flask import request, Blueprint, redirect, url_for, render_template, flash
from flask_login import login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from db.db_manager import CardDB
from db.user_manager import User

authentication_bp = Blueprint('authentication', __name__)

@authentication_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        # In a real app, use werkzeug.security to check password_hash
        manager = CardDB()
        user = manager.cursor.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        manager.close()

        if user and check_password_hash(user['password_hash'], password): # Replace with hash check
            user_obj = User(user['user_id'], user['username'], user['role'])
            remember_me = True if request.form.get('remember') else False
            login_user(user_obj, remember=remember_me)
            return redirect(url_for('inventory.inventory')) # Redirect to collection
        
        flash('Invalid username or password')
    return render_template('login.html')

@authentication_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('authentication.login'))

@authentication_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        role = request.form['role']
        admin_key = request.form.get('admin_key')
        
        hashed_pw = generate_password_hash(password)
        
        SECRET_ADMIN_PHRASE = os.environ.get('ADMIN_REGISTRATION_KEY')
        
        manager = CardDB()
        if role == "admin" and admin_key != SECRET_ADMIN_PHRASE:
            flash('Cannot create ADMIN account without site owner authorization')
            return redirect(url_for('authentication.login'))
        try:
            manager.cursor.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, hashed_pw, role)
            )
            manager.commit()
            flash('Account created! Please log in.')
            return redirect(url_for('authentication.login'))
        except Exception as e:
            print(f"Registration Error: {e}") # This will show the real error in your terminal
            flash('An error occurred during registration.')
            flash('Username already exists.')
        finally:
            manager.close()
            
    return render_template('register.html')