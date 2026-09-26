Airport OSM & WSAirports Importer — Installation Guide

A Blender add-on that generates high-accuracy base 3D airport models from the Wings Simulations WSAirports chunked database, with optional OpenStreetMap 3D buildings, extended markings, and island/hole polygon support.
1. Requirements

    Blender 3.0.0 or newer (tested up to current 4.x releases).

    An internet connection only if you enable OSM 3D Buildings (the add-on queries the Overpass API).

    The WSAirports database (see Step 2 below).

2. Download the WSAirports Database

You need the dataset separately from the add-on. Get it from:

https://github.com/WingsSimulations/wsairports-database

Pick either method:

Option A — Download ZIP (easiest)

    Open the repo link above.

    Click Code ▸ Download ZIP.

    Extract the archive somewhere permanent on your drive.

Option B — Git clone
bash

git clone https://github.com/WingsSimulations/wsairports-database.git

After extraction, verify the folder contains:
text

your-db-folder/
├── airports_index.json     ← master index
└── chunks/                 ← .wscairport chunk files

    You can also use the Setup Tutorial button inside the add-on panel to open these instructions at any time.

3. Install the Add-on in Blender

    Download the add-on file (e.g. airport_osm_importer.py or the packaged .zip).

    Open Blender.

    Go to Edit ▸ Preferences ▸ Add-ons.

    Click Install… (top-right).

    Select the add-on file (.py or .zip) and confirm.

    In the Add-ons list, search for "Airport OSM & WSAirports Importer" and tick the checkbox to enable it.

    If you installed a .zip and Blender complains, make sure the zip contains the .py file directly (not nested inside an extra folder).

4. First-Time Setup

    Open the 3D Viewport.

    Press N to open the sidebar.

    Click the Airport Pro tab (added by the add-on).

You'll see the panel with:

    Setup & Database Guide — quick access to the tutorial and GitHub repo.

    WSAirports Database — point this at your extracted DB folder.

    Include Layers — toggle which elements to generate.

    OpenStreetMap Integration — optional 3D buildings.

    Scale — model scale factor (default 0.1).

    Mandatory Attribution Notice — you must accept this to generate.

Steps

    Database Folder — click the folder icon and select the directory containing airports_index.json. If loaded correctly you'll see Index Ready: N airports in green.

    Target ICAO — enter the airport's ICAO code (e.g. KJFK).

    (Optional) OSM 3D Buildings — enable and set a search radius (default 3500 m).

    Accept the license/attribution terms — required for the Generate button to activate.

    Click Generate Airport Model.

The panel shows live progress while the airport is built.
5. What Gets Generated

A new collection named Airport_<ICAO> is created in the scene, containing sub-collections:
Collection	Contents
Runways	Runway surfaces
Taxiways	Taxiways and aprons
Aprons_Pavements	Apron pavements
Service_Roads	Roads
Markings	Line and paint markings
Helipads	Helipad surfaces
Windsocks	Windsock empties
Beacons	Beacon empties
Gates_Stands	Gate / ramp start empties
Jetways	Jetway empties
Buildings_OSM	OSM building extrusions (if enabled)

The add-on prints a summary (counts and elapsed time) when finished.
6. Licensing & Attribution (Important)

The generated content is derived from multiple GPLv2 / ODbL sources. Any scenery, export, or derivative you produce must credit:

    Laminar Research / X-Plane Scenery Gateway authors

    Wings Simulations

    OpenStreetMap contributors

Unattributed redistribution or commercial repackaging violates the GPLv2 license and is subject to DMCA takedown. Full terms are available via the View Full License & Terms button in the panel.
7. Troubleshooting
Problem	Fix
"Please select the root folder containing airports_index.json"	The Database Folder is wrong, empty, or not the extracted DB root.
"Airport 'XXXX' not found in master index"	Check the ICAO spelling; verify the airport exists in airports_index.json.
"Chunk file for '...' not found in database"	Re-extract the DB; a chunk file is missing.
"Overpass endpoints failed"	No internet, or the Overpass servers are busy. Retry, or disable OSM 3D Buildings.
Generate button greyed out	You haven't accepted the license terms yet.
Add-on doesn't appear in the list	Make sure you enabled it after installing, and that your Blender is 3.0+.
