from flask import Blueprint,request,flash,redirect,url_for, render_template, abort
from functools import wraps
from flask_login import login_required, current_user
from db.db_manager import CardDB

admin_bp = Blueprint('admin', __name__)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403) # Forbidden
        return f(*args, **kwargs)
    return decorated_function

@admin_bp.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    return render_template('admin.html')


@admin_bp.route('/add_locations', methods=['GET', 'POST'])
@login_required
def add_location():
    if request.method == 'POST':
        manager = CardDB()
        location_name = request.form.get('location_name')
        if location_name:
            manager.cursor.execute("INSERT INTO locations (name) VALUES (?)", (location_name,))
            manager.commit()
            flash(f"Location '{location_name}' added successfully!")
        return redirect(url_for('inventory.inventory'))
    return render_template("add_locations.html")