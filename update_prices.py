import time
import requests
from db.db_manager import CardDB # Assumes your manager is in db/

def update_all_prices():
    manager = CardDB()
    # Fetch all unique cards in your collection
    cards = manager.cursor.execute('''
                                   SELECT DISTINCT cp.scryfall_id 
                                   FROM card_printings cp
                                   JOIN inventory i ON cp.scryfall_id = i.scryfall_id ''').fetchall()
    
    for row in cards:
        sf_id = row['scryfall_id']
        # Respect Scryfall's rate limit (10 requests per second)
        time.sleep(0.1) 
        
        response = requests.get(f"https://api.scryfall.com/cards/{sf_id}")
        if response.status_code == 200:
            data = response.json()
            nonfoil = data.get('prices', {}).get('usd')
            foil = data.get('prices', {}).get('usd_foil')
            
            manager.cursor.execute(
                ''' 
                UPDATE card_printings
                SET current_price = ?, current_price_foil = ? 
                WHERE scryfall_id = ?
                ''', (nonfoil, foil, sf_id)
            )
            
            
            manager.cursor.execute('''
                INSERT INTO price_history (scryfall_id, price_usd, price_foil)
                VALUES (?, ?, ?)
            ''', (sf_id, nonfoil, foil)
            )
            print(f"updated {data["name"] }")
    
    manager.commit()
    manager.close()
    print("Price update complete.")

if __name__ == "__main__":
    update_all_prices()