function openEditModal(instanceId, locationId, isSurplus) {
    const modal = document.getElementById('editModal');
    const form = document.getElementById('editForm');
    
    // Set the form action to the correct instance ID
    form.action = `/edit_instance/${instanceId}`;
    
    // Pre-fill the fields
    document.getElementById('editLocation').value = locationId;
    document.getElementById('editSurplus').checked = (isSurplus === '1');
    
    modal.style.display = "block";
}

function closeModal() {
    document.getElementById('editModal').style.display = "none";
}

// Close if they click outside the box
window.onclick = function(event) {
    let modal = document.getElementById('editModal');
    if (event.target == modal) { closeModal(); }
}