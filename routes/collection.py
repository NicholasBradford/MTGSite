from flask import Blueprint, render_template, request
from db.db_manager import get_db
from services.feature_flags import require_feature


collection_bp = Blueprint('collection', __name__)

@collection_bp.route('/collection/planeswalkers')
@require_feature("planeswalker_collection")
def planeswalker_collection():
    name_query = request.args.get('name_query', '').strip().lower()

    page = request.args.get('page', 1, type=int)
    per_page = 50 
    offset = (page - 1) * per_page

    manager = get_db()
    binder_location_id = 97
    
    base_query = '''
        FROM planeswalker_tracker pt

        LEFT JOIN card_printings cp_default 
            ON pt.default_scryfall_id = cp_default.scryfall_id

        LEFT JOIN (
            SELECT 
                i.scryfall_id, 
                i.finish, 
                cp.oracle_id
            FROM inventory i
            JOIN card_printings cp 
                ON i.scryfall_id = cp.scryfall_id
            WHERE i.location_id = ? 
            GROUP BY cp.oracle_id 
        ) binder_inv 
            ON pt.oracle_id = binder_inv.oracle_id

        LEFT JOIN card_printings cp_binder 
            ON binder_inv.scryfall_id = cp_binder.scryfall_id
    '''

    params = [binder_location_id]
    
    if name_query:
        base_query += " WHERE LOWER(pt.name) LIKE ?"
        params.append(f"%{name_query}%")
        
    count_query = f"SELECT COUNT(pt.oracle_id) {base_query}"
    total_items = manager.cursor.execute(count_query, params).fetchone()[0]
    total_pages = (total_items + per_page - 1) // per_page
    
    main_query = f'''
        SELECT 
            pt.oracle_id,
            pt.name,
            pt.release_date,
            pt.default_scryfall_id,
            cp_default.image_url AS default_image,
            
            binder_inv.scryfall_id AS binder_scryfall_id,
            binder_inv.finish AS binder_finish,
            cp_binder.image_url AS binder_image,

            (
                SELECT l.name
                FROM inventory i3
                JOIN card_printings cp3 
                    ON i3.scryfall_id = cp3.scryfall_id
                JOIN locations l 
                    ON i3.location_id = l.location_id
                WHERE cp3.oracle_id = pt.oracle_id
                  AND i3.location_id != ?
                ORDER BY i3.added DESC
                LIMIT 1
            ) AS other_location,

            (
                SELECT COUNT(i2.instance_id) 
                FROM inventory i2 
                JOIN card_printings cp2 
                    ON i2.scryfall_id = cp2.scryfall_id 
                WHERE cp2.oracle_id = pt.oracle_id
            ) AS total_owned

        {base_query}
        ORDER BY pt.sort_index ASC
        LIMIT ? OFFSET ?
    '''
    
    query_params = [binder_location_id] + params + [per_page, offset]

    planeswalkers = manager.cursor.execute(main_query, query_params).fetchall()
    manager.close()
    
    pw_list = [dict(row) for row in planeswalkers]
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('_pw_items.html', planeswalkers=pw_list)

    return render_template(
        'planeswalkers.html', 
        planeswalkers=pw_list,
        page=page,
        total_pages=total_pages,
        name_query=name_query
    )