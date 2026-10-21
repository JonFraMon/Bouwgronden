"""
bouwgrond_shortlist.py
======================
Maakt een gerangschikte shortlist van onbebouwde percelen in woongebied
rond Duffel, Kontich, Lint en Sint-Katelijne-Waver.

Hoe het werkt (de "pijplijn"):
  1. Gemeentegrenzen ophalen            (Vlaamse overheid, WFS)
  2. Kadastrale percelen ophalen        (GRB - Adp, WFS)
  3. Alleen percelen in woongebied      (gewestplan, WFS)
  4. Percelen met een gebouw schrappen  (GRB - Gbg, WFS)
  5. Te kleine/grote percelen schrappen (MIN_/MAX_OPPERVLAKTE)
  6. Alleen open bebouwing (schatting)  (breedte + vrijstaande buurhuizen)
  7. Afstanden berekenen tot scholen, supermarkten, station,
     dorpskern en drukke wegen          (OpenStreetMap, Overpass API)
  8. Dichtstbijzijnd adres per perceel  (Adressenregister, WFS)
  9. Score 0-100 per perceel en exporteren naar CSV + interactieve kaart

WFS = "Web Feature Service": een standaard geo-API. Je vraagt een kaartlaag
op binnen een rechthoek (bbox) en krijgt vormen terug (hier als GeoJSON).

Gebruik:
  pip install geopandas folium requests pyproj
  python bouwgrond_shortlist.py --discover    # eerst: controleer de lagen
  python bouwgrond_shortlist.py               # daarna: echte run
"""

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from io import BytesIO

import geopandas as gpd
import pandas as pd
import requests
from pyproj import Transformer
from shapely.geometry import LineString, Point

# ---------------------------------------------------------------------------
# INSTELLINGEN — pas deze aan naar jouw wensen
# ---------------------------------------------------------------------------
GEMEENTEN = ["Duffel", "Kontich", "Lint", "Sint-Katelijne-Waver"]

MIN_OPPERVLAKTE = 550       # m², kleinere percelen vallen af
MAX_OPPERVLAKTE = 1000      # m², grotere percelen vallen af (weides, grote tuinen)
IDEALE_OPPERVLAKTE = 1000   # m², vanaf hier volle punten voor oppervlakte
MAX_GEBOUW_M2 = 15          # m² gebouw op perceel toegestaan (tuinhuisje, ruis)

# Open bebouwing (vrijstaand). Geen dataset zegt dit voor lege percelen, dus
# we schatten het: zijn de huizen in de buurt vrijstaand, en is het perceel
# breed genoeg voor een huis met 3 m vrije strook aan beide kanten?
MIN_BREEDTE = 15            # m, korte zijde van het perceel
OPEN_STRAAL = 100           # m rond het perceel waarin we buurhuizen bekijken
MIN_PCT_OPEN = 70           # % van die buurhuizen dat vrijstaand moet zijn
TOP_N = 10                  # hoeveel percelen in de shortlist

# Gewicht van elk criterium in de eindscore (hoeft niet op te tellen tot 1)
GEWICHTEN = {
    "oppervlakte": 1.0,
    "rust":        1.0,   # ver van drukke wegen
    "centrum":     1.0,
    "station":     1.0,
    "school":      1.0,
    "supermarkt":  1.0,
}

# Afstandsgrenzen in meter: (volle punten tot, nul punten vanaf)
AFSTANDEN = {
    "centrum":    (500, 4000),
    "station":    (1000, 6000),
    "school":     (500, 3000),
    "supermarkt": (500, 3000),
}
# Rust: binnen RUST_MIN m van een drukke weg = 0 punten, vanaf RUST_MAX = vol
RUST_MIN, RUST_MAX = 50, 400

# Welke gewestplanbestemmingen tellen als bouwgrond voor wonen
WOON_WOORDEN = ["woongebied", "woonpark", "wonen"]
NIET_WOORDEN = ["uitbreiding", "reserve"]   # niet direct bebouwbaar

# Lambert 72: Belgisch coördinatenstelsel in meter (handig voor m² en afstanden)
CRS = "EPSG:31370"

# WFS-diensten. 'zoek' = trefwoord om de juiste laag te vinden in de
# GetCapabilities (de inhoudstafel van de dienst). Weet je de exacte laagnaam
# al (via --discover), vul die dan in bij 'laag'.
SERVICES = {
    "gemeenten":  {"url": "https://geo.api.vlaanderen.be/VRBG/wfs",
                   "zoek": ["refgem"], "laag": None},
    "percelen":   {"url": "https://geo.api.vlaanderen.be/GRB/wfs",
                   "zoek": ["adp"], "laag": None},
    "gebouwen":   {"url": "https://geo.api.vlaanderen.be/GRB/wfs",
                   "zoek": ["gbg"], "laag": None},
    "bestemming": {"url": "https://www.mercator.vlaanderen.be/raadpleegdienstenmercatorpubliek/wfs",
                   "zoek": ["gwp", "gewestplan"], "laag": "lu:lu_gwp_gv"},
    "adressen":   {"url": "https://geo.api.vlaanderen.be/Adressenregister/wfs",
                   "zoek": ["adres"], "laag": "Adressenregister:Adres"},
}

# Achtergrondkaarten van Digitaal Vlaanderen (WMTS). De tegelservers van
# OpenStreetMap zelf blokkeren kaarten die als lokaal bestand geopend worden.
WMTS_TEGEL = ("https://geo.api.vlaanderen.be/{dienst}/wmts?SERVICE=WMTS&REQUEST=GetTile"
              "&VERSION=1.0.0&LAYER={laag}&STYLE=&FORMAT=image/png"
              "&TILEMATRIXSET=GoogleMapsVL&TILEMATRIX={{z}}&TILEROW={{y}}&TILECOL={{x}}")
ACHTERGRONDEN = {
    "Basiskaart (GRB)": WMTS_TEGEL.format(dienst="GRB", laag="grb_bsk"),
    "Luchtfoto":        WMTS_TEGEL.format(dienst="OMWRGBMRVL", laag="omwrgbmrvl"),
}
ADRES_STATUSSEN = ["InGebruik", "Voorgesteld"]   # geen gehistoreerde adressen

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "bouwgrond-shortlist/1.0 (persoonlijk gebruik)"}
PAGINA = 5000   # features per WFS-verzoek


# ---------------------------------------------------------------------------
# WFS-hulpfuncties
# ---------------------------------------------------------------------------
def lagen_van_dienst(url):
    """Vraag de inhoudstafel (GetCapabilities) op en geef alle laagnamen."""
    r = requests.get(url, params={"service": "WFS", "request": "GetCapabilities"},
                     headers=HEADERS, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    namen = []
    for el in root.iter():
        if el.tag.endswith("FeatureType"):
            for kind in el:
                if kind.tag.endswith("Name") and kind.text:
                    namen.append(kind.text.strip())
    return namen


def kies_laag(sleutel):
    """Gebruik de ingestelde laag, of zoek er een op trefwoord."""
    cfg = SERVICES[sleutel]
    if cfg["laag"]:
        return cfg["laag"]
    namen = lagen_van_dienst(cfg["url"])
    for woord in cfg["zoek"]:
        hits = [n for n in namen if woord.lower() in n.lower()]
        if hits:
            cfg["laag"] = min(hits, key=len)   # kortste naam = meestal de basislaag
            print(f"  [{sleutel}] laag gekozen: {cfg['laag']}")
            return cfg["laag"]
    sys.exit(f"Geen laag gevonden voor '{sleutel}'. Draai --discover en vul "
             f"SERVICES['{sleutel}']['laag'] handmatig in.")


def wfs_ophalen(sleutel, bbox, max_features=None):
    """Haal alle features op binnen een bbox, pagina per pagina."""
    url, laag = SERVICES[sleutel]["url"], kies_laag(sleutel)
    delen, start = [], 0
    while True:
        aantal = PAGINA if max_features is None else min(PAGINA, max_features)
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": laag, "srsName": CRS,
            "bbox": ",".join(f"{v:.1f}" for v in bbox) + f",{CRS}",
            "outputFormat": "application/json",
            "count": aantal, "startIndex": start,
        }
        r = requests.get(url, params=params, headers=HEADERS, timeout=180)
        if r.status_code != 200 or not r.content.lstrip().startswith(b"{"):
            sys.exit(f"WFS-fout bij {sleutel}: {r.status_code}\n{r.text[:500]}")
        deel = gpd.read_file(BytesIO(r.content))
        if deel.empty:
            break
        delen.append(deel)
        print(f"  [{sleutel}] {start + len(deel)} features opgehaald...")
        if len(deel) < aantal or max_features is not None:
            break
        start += aantal
    if not delen:
        return gpd.GeoDataFrame(geometry=[], crs=CRS)
    gdf = pd.concat(delen, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry")
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS)
    return gdf.to_crs(CRS)


def tekstkolommen(gdf):
    return [c for c in gdf.columns if c != "geometry"
            and (gdf[c].dtype == object or pd.api.types.is_string_dtype(gdf[c]))]


# ---------------------------------------------------------------------------
# Stappen van de pijplijn
# ---------------------------------------------------------------------------
def grove_bbox():
    """Ruwe rechthoek rond de vier gemeenten, omgezet naar Lambert 72."""
    t = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
    x1, y1 = t.transform(4.38, 51.02)
    x2, y2 = t.transform(4.62, 51.17)
    return (x1, y1, x2, y2)


def gemeenten_ophalen():
    gdf = wfs_ophalen("gemeenten", grove_bbox())
    for kol in tekstkolommen(gdf):
        match = gdf[gdf[kol].isin(GEMEENTEN)]
        if len(match) == len(GEMEENTEN):
            return match
    sys.exit(f"Kon de gemeenten niet vinden. Kolommen: {list(gdf.columns)}")


def filter_woongebied(percelen, bestemming):
    """Houd percelen waarvan het middelpunt in een woonbestemming ligt."""
    def is_woon(rij):
        tekst = " ".join(str(rij[k]) for k in tekstkolommen(bestemming)).lower()
        return (any(w in tekst for w in WOON_WOORDEN)
                and not any(n in tekst for n in NIET_WOORDEN))

    woon = bestemming[bestemming.apply(is_woon, axis=1)]
    print(f"  {len(woon)} woonzones gevonden in het gewestplan")
    punten = percelen.copy()
    punten["geometry"] = percelen.representative_point()
    binnen = gpd.sjoin(punten, woon[["geometry"]], predicate="within")
    return percelen.loc[binnen.index.unique()]


def filter_onbebouwd(percelen, gebouwen):
    """Schrap percelen waar meer dan MAX_GEBOUW_M2 gebouw op staat."""
    if gebouwen.empty:
        return percelen
    paren = gpd.sjoin(percelen[["geometry"]], gebouwen[["geometry"]],
                      predicate="intersects")
    perc_geo = percelen.geometry.loc[paren.index].values
    geb_geo = gebouwen.geometry.loc[paren["index_right"]].values
    overlap = pd.Series(
        [p.intersection(g).area for p, g in zip(perc_geo, geb_geo)],
        index=paren.index)
    bebouwd = overlap.groupby(level=0).sum()
    bebouwd = bebouwd[bebouwd > MAX_GEBOUW_M2].index
    return percelen.drop(index=bebouwd)


def breedte(geom):
    """Korte zijde van de kleinste rechthoek rond het perceel: ruwe maat voor
    de breedte. Meet de vorm, niet de echte straatkant."""
    hoeken = list(geom.minimum_rotated_rectangle.exterior.coords)
    return min(Point(hoeken[i]).distance(Point(hoeken[i + 1])) for i in range(2))


def pct_open_buurt(percelen, gebouwen):
    """% hoofdgebouwen binnen OPEN_STRAAL m dat vrijstaand is (geen ander
    hoofdgebouw raakt). Leeg (NaN) als er geen huizen in de buurt staan."""
    huizen = gebouwen.loc[gebouwen["LBLTYPE"] == "hoofdgebouw", ["geometry"]]
    huizen = huizen.reset_index(drop=True)
    # 0,2 m marge: aanpalende huizen delen een muur, maar de tekening is niet altijd exact
    marge = huizen.assign(geometry=huizen.buffer(0.2))
    raakt = gpd.sjoin(huizen, marge, predicate="intersects")
    raakt = raakt[raakt.index != raakt["index_right"]]
    huizen["open"] = ~huizen.index.isin(raakt.index)

    zones = percelen[["geometry"]].assign(geometry=percelen.buffer(OPEN_STRAAL))
    buren = gpd.sjoin(zones, huizen, predicate="intersects")
    return (buren.groupby(level=0)["open"].mean() * 100).reindex(percelen.index)


def osm_ophalen(bbox_lambert, marge=3000):
    """Haal voorzieningen en drukke wegen op uit OpenStreetMap."""
    t = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    w, s = t.transform(bbox_lambert[0] - marge, bbox_lambert[1] - marge)
    e, n = t.transform(bbox_lambert[2] + marge, bbox_lambert[3] + marge)
    b = f"{s},{w},{n},{e}"
    poi_query = f"""[out:json][timeout:180];
    ( nwr["amenity"="school"]({b});
      nwr["shop"="supermarket"]({b});
      nwr["railway"="station"]({b});
      node["place"~"^(town|village)$"]({b}); );
    out center tags;"""
    weg_query = f"""[out:json][timeout:180];
    way["highway"~"^(motorway|trunk|primary|secondary)$"]({b});
    out geom;"""

    def vraag(q):
        # Overpass is een gratis vrijwilligersdienst die vaak vol zit (502/504/429).
        # De dienst vraagt dan zelf om opnieuw te proberen met steeds langer wachten.
        for poging, wacht in enumerate([30, 60, 120, 240, None], start=1):
            r = requests.post(OVERPASS_URL, data={"data": q}, headers=HEADERS, timeout=300)
            if r.status_code not in (429, 502, 503, 504) or wacht is None:
                break
            print(f"  Overpass is druk ({r.status_code}), poging {poging}: "
                  f"opnieuw over {wacht} s...")
            time.sleep(wacht)
        r.raise_for_status()
        return r.json()["elements"]

    rijen = []
    for el in vraag(poi_query):
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        tags = el.get("tags", {})
        if lat is None:
            continue
        if tags.get("amenity") == "school":
            soort = "school"
        elif tags.get("shop") == "supermarket":
            soort = "supermarkt"
        elif tags.get("railway") == "station":
            soort = "station"
        else:
            soort = "centrum"
        rijen.append({"soort": soort, "naam": tags.get("name", ""),
                      "geometry": Point(lon, lat)})
    pois = gpd.GeoDataFrame(rijen, crs="EPSG:4326").to_crs(CRS)

    lijnen = [LineString([(p["lon"], p["lat"]) for p in el["geometry"]])
              for el in vraag(weg_query) if len(el.get("geometry", [])) > 1]
    wegen = gpd.GeoDataFrame(geometry=lijnen, crs="EPSG:4326").to_crs(CRS)
    print(f"  OSM: {len(pois)} voorzieningen, {len(wegen)} drukke wegsegmenten")
    return pois, wegen


def afstand_tot(percelen, doelen):
    """Afstand (m) van elk perceel tot het dichtstbijzijnde doel."""
    if doelen.empty:
        return pd.Series(float("inf"), index=percelen.index)
    res = gpd.sjoin_nearest(percelen[["geometry"]], doelen[["geometry"]],
                            distance_col="afstand")
    return res.groupby(level=0)["afstand"].min()


def adres_dichtbij(percelen, adressen):
    """Dichtstbijzijnd officieel adres per perceel, plus de afstand ernaartoe.
    Onbebouwde percelen hebben zelden een eigen adres; afstand 0 betekent dat
    het adres op het perceel zelf ligt, anders is het meestal dat van de buren."""
    adressen = adressen[adressen["AdresStatus"].isin(ADRES_STATUSSEN)]
    res = gpd.sjoin_nearest(percelen[["geometry"]],
                            adressen[["VolledigAdres", "geometry"]],
                            distance_col="afstand")
    res = res[~res.index.duplicated()]   # gelijke afstand (bv. busnummers): eerste houden
    return res["VolledigAdres"], res["afstand"]


def lineair(waarde, goed, slecht):
    """1 als waarde <= goed, 0 als >= slecht, rechte lijn ertussen."""
    if waarde <= goed:
        return 1.0
    if waarde >= slecht:
        return 0.0
    return 1 - (waarde - goed) / (slecht - goed)


def scoren(df):
    df = df.copy()
    df["s_oppervlakte"] = df["oppervlakte_m2"].apply(
        lambda a: 1 - lineair(a, MIN_OPPERVLAKTE, IDEALE_OPPERVLAKTE))
    df["s_rust"] = df["afst_drukke_weg"].apply(
        lambda d: 1 - lineair(d, RUST_MIN, RUST_MAX))
    for k, (goed, slecht) in AFSTANDEN.items():
        df[f"s_{k}"] = df[f"afst_{k}"].apply(lambda d: lineair(d, goed, slecht))
    totaal = sum(GEWICHTEN.values())
    df["score"] = sum(df[f"s_{k}"] * w for k, w in GEWICHTEN.items()) / totaal * 100
    df["score"] = df["score"].round(1)
    return df.sort_values("score", ascending=False)


def kaart_maken(df, pad):
    import folium
    geo = df.to_crs("EPSG:4326")
    midden = geo.geometry.union_all().centroid
    m = folium.Map(location=[midden.y, midden.x], zoom_start=13, tiles=None)
    for naam, url in ACHTERGRONDEN.items():
        folium.TileLayer(url, name=naam, attr="© Digitaal Vlaanderen",
                         max_zoom=21).add_to(m)
    id_kol = next((c for c in df.columns if "capakey" in c.lower()), None)
    for rang, (_, rij) in enumerate(geo.iterrows(), start=1):
        top = rang <= TOP_N
        kleur = "#d7301f" if top else "#3182bd"
        adres = rij["adres"]
        if rij["afst_adres"] >= 1:
            adres += f" (dichtstbij, {rij['afst_adres']:.0f} m)"
        info = (f"<b>#{rang} — score {rij['score']}</b><br>"
                f"{'CaPaKey: ' + str(rij[id_kol]) + '<br>' if id_kol else ''}"
                f"Adres: {adres}<br>"
                f"Gemeente: {rij['gemeente']}<br>"
                f"Oppervlakte: {rij['oppervlakte_m2']:.0f} m² · Breedte: {rij['breedte_m']:.0f} m<br>"
                f"Vrijstaande huizen in de buurt: {rij['pct_open_buurt']:.0f}%<br>"
                f"Centrum: {rij['afst_centrum']:.0f} m · Station: {rij['afst_station']:.0f} m<br>"
                f"School: {rij['afst_school']:.0f} m · Supermarkt: {rij['afst_supermarkt']:.0f} m<br>"
                f"Drukke weg: {rij['afst_drukke_weg']:.0f} m")
        folium.GeoJson(rij.geometry.__geo_interface__,
                       style_function=lambda _, k=kleur, t=top: {
                           "color": k, "weight": 3 if t else 1,
                           "fillOpacity": 0.6 if t else 0.25},
                       popup=folium.Popup(info, max_width=320)).add_to(m)
    folium.LayerControl().add_to(m)   # knop rechtsboven: basiskaart of luchtfoto
    m.save(pad)


# ---------------------------------------------------------------------------
# Hoofdprogramma
# ---------------------------------------------------------------------------
def discover():
    """Toon welke lagen en kolommen elke dienst heeft (om te controleren)."""
    for sleutel, cfg in SERVICES.items():
        print(f"\n=== {sleutel}: {cfg['url']}")
        try:
            namen = lagen_van_dienst(cfg["url"])
        except Exception as e:
            print(f"  Fout: {e}")
            continue
        relevant = [n for n in namen if any(w in n.lower() for w in cfg["zoek"])]
        print(f"  {len(namen)} lagen, relevant: {relevant or '(geen match)'}")
        if relevant:
            cfg["laag"] = min(relevant, key=len)
            voorbeeld = wfs_ophalen(sleutel, grove_bbox(), max_features=3)
            print(f"  Kolommen van {cfg['laag']}: {list(voorbeeld.columns)}")
            for k in tekstkolommen(voorbeeld)[:4]:
                print(f"    {k}: {voorbeeld[k].head(3).tolist()}")


def main():
    print("1. Gemeentegrenzen")
    gem = gemeenten_ophalen()
    naam_kol = next(k for k in tekstkolommen(gem) if gem[k].isin(GEMEENTEN).all())
    gebied = gem.geometry.union_all()
    bbox = gem.total_bounds

    print("2. Kadastrale percelen")
    perc = wfs_ophalen("percelen", bbox)
    punten = perc.representative_point()
    perc = perc[punten.within(gebied)].copy()
    print(f"  {len(perc)} percelen binnen de vier gemeenten")

    print("3. Woongebied (gewestplan)")
    perc = filter_woongebied(perc, wfs_ophalen("bestemming", bbox))
    print(f"  {len(perc)} percelen in woongebied")

    print("4. Gebouwen")
    gebouwen = wfs_ophalen("gebouwen", bbox)
    perc = filter_onbebouwd(perc, gebouwen)
    print(f"  {len(perc)} onbebouwde percelen")

    print("5. Oppervlakte")
    perc["oppervlakte_m2"] = perc.geometry.area
    perc = perc[perc["oppervlakte_m2"].between(MIN_OPPERVLAKTE, MAX_OPPERVLAKTE)].copy()
    print(f"  {len(perc)} percelen tussen {MIN_OPPERVLAKTE} en {MAX_OPPERVLAKTE} m²")
    if perc.empty:
        sys.exit("Geen percelen over. Verruim MIN/MAX_OPPERVLAKTE of controleer de filters.")

    print("6. Open bebouwing (schatting)")
    perc["breedte_m"] = perc.geometry.apply(breedte)
    perc["pct_open_buurt"] = pct_open_buurt(perc, gebouwen)
    perc = perc[(perc["breedte_m"] >= MIN_BREEDTE)
                & (perc["pct_open_buurt"] >= MIN_PCT_OPEN)].copy()
    print(f"  {len(perc)} percelen van minstens {MIN_BREEDTE} m breed, in een buurt "
          f"met minstens {MIN_PCT_OPEN}% vrijstaande huizen")
    if perc.empty:
        sys.exit("Geen percelen over. Verlaag MIN_BREEDTE of MIN_PCT_OPEN.")

    gem_punten = perc.representative_point().to_frame("geometry").set_geometry("geometry")
    perc["gemeente"] = gpd.sjoin(gem_punten, gem[[naam_kol, "geometry"]],
                                 predicate="within")[naam_kol]

    print("7. Voorzieningen (OpenStreetMap)")
    pois, wegen = osm_ophalen(bbox)
    for soort in AFSTANDEN:
        perc[f"afst_{soort}"] = afstand_tot(perc, pois[pois["soort"] == soort])
    perc["afst_drukke_weg"] = afstand_tot(perc, wegen)

    print("8. Adressen (Adressenregister)")
    perc["adres"], perc["afst_adres"] = adres_dichtbij(perc, wfs_ophalen("adressen", bbox))

    print("9. Score en export")
    resultaat = scoren(perc)
    resultaat.insert(0, "rang", range(1, len(resultaat) + 1))
    resultaat.drop(columns="geometry").to_csv("bouwgronden_alle.csv", index=False)
    resultaat.head(TOP_N).drop(columns="geometry").to_csv("bouwgronden_shortlist.csv", index=False)
    resultaat.to_crs("EPSG:4326").to_file("bouwgronden.geojson", driver="GeoJSON")
    kaart_maken(resultaat, "bouwgronden_kaart.html")
    print(f"\nKlaar! {len(resultaat)} percelen gerangschikt.")
    print("  bouwgronden_shortlist.csv  — je top", TOP_N)
    print("  bouwgronden_alle.csv       — alle percelen met scores")
    print("  bouwgronden_kaart.html     — open in je browser (top in rood)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--discover", action="store_true",
                   help="toon beschikbare lagen en kolommen, zonder echte run")
    args = p.parse_args()
    discover() if args.discover else main()
