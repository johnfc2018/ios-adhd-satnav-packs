# ios-adhd-satnav packs

Offline data packs for the satnav app: Valhalla routing tiles, a Protomaps basemap, and a SQLite index
(speed cameras, speed limits, lit roads, place search) per region.

The app reads the list of regions from
`https://github.com/johnfc2018/ios-adhd-satnav-packs/releases/download/manifest/regions.json`.

Run the **Build and publish region packs** workflow to (re)build a region from `tools/regions.json`.

Data © OpenStreetMap contributors, licensed under the [ODbL](https://www.openstreetmap.org/copyright).
Basemap tiles from [Protomaps](https://protomaps.com). Routing tiles built with [Valhalla](https://github.com/valhalla/valhalla).
