from flask import Blueprint,request,flash,redirect,url_for, render_template
from flask_login import login_required
from db.db_manager import CardDB

admin_bp = Blueprint('admin', __name__)

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