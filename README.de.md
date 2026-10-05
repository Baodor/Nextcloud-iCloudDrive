# iCloud Drive Bridge für Nextcloud

[English](README.md) · [Installation](#installation-bei-einer-bestehenden-docker-nextcloud) · [Einstellungen](#einstellungen-je-ordnerpaar) · [Fehlerbehebung](#fehlerbehebung)

Apple iCloud Drive in Nextcloud durchsuchen und ausgewählte Ordner täglich oder in einem Intervall synchronisieren. Eine native Nextcloud-App stellt die Oberfläche bereit; ein interner Docker-Dienst verwendet rclone für Apple-Anmeldung und Dateioperationen.

**Version 0.1.0 ist eine erste experimentelle Fassung.** Auch rclone stuft sein iCloud-Backend als experimentell ein. Die echte Apple-Anmeldung und der erweiterte Datenschutz hängen vom jeweiligen Konto ab und müssen beim ersten Einsatz geprüft werden. Im Repository befinden sich keine Apple-Zugangsdaten. Das Projekt ist unabhängig von Apple, Nextcloud und rclone.

![Oberfläche mit Demonstrationsdaten](docs/images/overview.png)

Das Bild zeigt die ausgelieferte Oberfläche mit definierten Testdaten, keine angemeldete Apple-Verbindung.

## Funktionsumfang

| Funktion | Verhalten |
|---|---|
| Native Oberfläche | Übersicht, Verbindungen, Ordnerabgleich, Aktivität und Administration |
| Deutsch und Englisch | Sprache richtet sich nach der Nextcloud-Dokumentsprache |
| Ordnerauswahl | iCloud- und Nextcloud-Ordner durchsuchen; Nextcloud-Zielordner erstellen |
| Beide Richtungen | `rclone bisync`, einschließlich Änderungen und Löschungen auf beiden Seiten |
| Import | `rclone copy` von iCloud nach Nextcloud; Quelllöschungen werden nicht übertragen |
| Export | `rclone copy` von Nextcloud nach iCloud; Quelllöschungen werden nicht übertragen |
| Zeitpläne | Manuell, ausgewählte Wochentage zu einer Uhrzeit oder alle 60–10.080 Minuten |
| Vorschau | Probelauf mit einer getrennten Kopie des bisync-Arbeitszustands |
| Schutz | Prüfdateien, Löschgrenze und Ablehnung überlappender Ordnerzuordnungen |
| Konflikte | Beide Fassungen erhalten oder eine Fassung bevorzugen und die andere als Kopie behalten |
| Sicherungen | Ersetzte und gelöschte Fassungen außerhalb der Abgleichordner archivieren |
| Laufüberwachung | Übertragungsfortschritt, Stoppen, Protokolle und die letzten 100 Läufe je Benutzer |
| Direkter Zugriff | WebDAV mit Lesezugriff für Nextclouds externen Speicher |
| Mehrere Benutzer | Serverseitig an den angemeldeten Nextcloud-Benutzer gebundene Daten und Zuordnungen |

## Aufbau

Der Browser kommuniziert ausschließlich mit angemeldeten Nextcloud-OCS-Routen. Nextcloud leitet erlaubte Anfragen mit einem Dienst-Token und der serverseitig festgelegten Benutzerkennung an die Bridge weiter. Die Bridge verwaltet Zeitpläne, SQLite-Metadaten, verschlüsselte Zugangsdaten, rclone-Konfiguration und Übertragungsprozesse.

Der Abgleich nutzt die reguläre Nextcloud-WebDAV-Schnittstelle. Die Bridge benötigt keinen Zugriff auf das Nextcloud-Datenverzeichnis, dessen Datenbank, den Docker-Socket oder das Host-Dateisystem. Die Dateien werden über Nextclouds normalen Dateizugriff verarbeitet. Der Zeitplan der Bridge läuft unabhängig von Nextclouds AJAX- oder Cron-Hintergrundmodus.

Für die direkte Dateiansicht startet je aktivem Benutzer ein eigener rclone-WebDAV-Prozess auf Loopback. Das Gateway prüft dessen separate Einbindungs-Zugangsdaten vor jeder Weiterleitung. Die Dateien bleiben in iCloud; ein temporärer VFS-Cache puffert gelesene Inhalte. Dateien bearbeitest du in den synchronisierten Nextcloud-Ordnern.

## Voraussetzungen und Kompatibilität

- Bestehende Nextcloud mit PHP ab 8.1. Die App-Metadaten akzeptieren **Nextcloud 30–35**. Die CI-Prüfung deckt 30 und 35 ab; weitere Versionen innerhalb des Bereichs benötigen ebenfalls einen Installationstest.
- Docker Engine und Docker-Compose-Plugin unter Linux für die Bridge.
- Python 3 auf dem Host zur Erzeugung der Schlüssel; dort sind keine zusätzlichen Python-Pakete erforderlich.
- Apple-Account mit iCloud Drive und Zwei-Faktor-Authentifizierung.
- Ausgehender HTTPS-/DNS-Zugriff der Bridge auf Apple und Erreichbarkeit der Nextcloud-WebDAV-URL.
- Gemeinsames Docker-Netz oder eine private, von Nextcloud erreichbare Bridge-Adresse.
- Genügend Nextcloud-Speicher für Dateikopien und Sicherungen.

JavaScript und CSS werden fertig mitgeliefert. Zur Installation brauchst du weder Node/npm noch Composer, FUSE oder einen privilegierten Container. Das Dockerfile verwendet rclone **1.75.1** und die Python-3.12-Imageserie. Zugangsdaten und Laufzeitdaten liegen außerhalb von Git.

## Installation bei einer bestehenden Docker-Nextcloud

### 1. Projekt klonen

```bash
sudo install -d -o "$(id -u)" -g "$(id -g)" /mnt/docker/compose/Nextcloud-iCloudDrive
git clone https://github.com/Baodor/Nextcloud-iCloudDrive.git /mnt/docker/compose/Nextcloud-iCloudDrive
cd /mnt/docker/compose/Nextcloud-iCloudDrive
cp .env.example .env
```

Ermittle den Nextcloud-Anwendungscontainer und seine Netze:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}'
docker inspect NEXTCLOUD_CONTAINER --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name}}{{end}}'
```

Ersetze `NEXTCLOUD_CONTAINER` durch den Anwendungscontainer. Datenbank-, Redis-, Cron- und AIO-Mastercontainer sind dafür ungeeignet. Trage in `.env` die tatsächlichen Werte ein:

```dotenv
NEXTCLOUD_URL=https://cloud.example.com
NEXTCLOUD_NETWORK=dein_bestehendes_docker_netz
TZ=Europe/Berlin
BRIDGE_MAX_DAV_USERS=20
```

`NEXTCLOUD_URL` ist die Basisadresse deiner Nextcloud, gegebenenfalls mit Installations-Unterverzeichnis. Die Bridge ergänzt `/remote.php/dav/files/ANMELDE_ID/`. Diese Adresse muss ohne interaktive SSO- oder Reverse-Proxy-Anmeldeseite erreichbar sein. Die Anmeldung erfolgt mit dem Nextcloud-App-Passwort. Internes HTTP im vertrauenswürdigen Docker-Netz wird unterstützt; über andere Netze HTTPS verwenden.

Das angegebene Netzwerk ist extern: Compose erstellt es nicht und hängt den vorhandenen Nextcloud-Container nicht automatisch ein. Der Docker-DNS-Name der Bridge lautet `icloud-bridge`.

### 2. Schlüssel erzeugen und Bridge starten

```bash
python3 scripts/init-secrets.py
sudo chown -R 10001:10001 secrets
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=50 icloud-bridge
```

Das Skript behält vorhandene Schlüssel bei. Erzeuge nach dem Speichern von Zugangsdaten keinen neuen Verschlüsselungsschlüssel. Das Schlüsselverzeichnis ist nur für seinen Eigentümer zugänglich; Docker bindet die einzelnen Dateien für UID 10001 ein. Unter dieser UID läuft die Bridge ohne zusätzliche Linux-Capabilities und mit schreibgeschütztem Root-Dateisystem. Das persistente Volume unter `/data` bleibt beschreibbar.

**Sichere das Verzeichnis `secrets` zusammen mit dem Compose-Datenvolume.** Zur Wiederherstellung werden der Schlüssel und die verschlüsselte Konfiguration gemeinsam benötigt. Bei Schlüsselverlust musst du Konten erneut anmelden. Bewahre vorhandene bisync-Zustände auf und prüfe die Ordner, bevor du neu initialisierst.

### 3. App in Nextcloud installieren

```bash
bash scripts/install-nextcloud-app.sh NEXTCLOUD_CONTAINER
```

Das Skript erkennt übliche Pfade offizieller Nextcloud- und LinuxServer-Container, den PHP-Dienstbenutzer und das konfigurierte beschreibbare App-Verzeichnis. Es kopiert die App unter dem vorgeschriebenen Ordnernamen `icloud_drive`, bewahrt einen vorhandenen App-Ordner vorübergehend auf und ruft `occ app:enable icloud_drive` auf. Schlägt die Aktivierung fehl, wird der bisherige App-Ordner zurückgestellt.

Bei einer abweichenden Struktur kannst du die Werte festlegen:

```bash
NEXTCLOUD_ROOT=/var/www/html \
NEXTCLOUD_APP_DIR=/var/www/html/custom_apps \
NEXTCLOUD_USER=www-data \
bash scripts/install-nextcloud-app.sh NEXTCLOUD_CONTAINER
```

Das App-Verzeichnis sollte auf persistentem Speicher liegen. Andernfalls installierst du die App nach einem Austausch des Nextcloud-Containers erneut. Das Skript nennt den temporären Sicherungspfad; diese Sicherung verschwindet beim Austausch ihres Containers.

### 4. Nextcloud mit der Bridge verbinden

Öffne als Nextcloud-Administrator **iCloud Drive → Administration**:

- Interne Dienst-URL: `http://icloud-bridge:8080`
- API-Token: Inhalt der Datei `secrets/api_token`

Den Token liest du lokal nach der Eigentümerumstellung so aus:

```bash
sudo cat secrets/api_token
```

Füge ihn in das Formular ein und speichere. Token nicht in Issues, Screenshots oder Chat-Protokolle kopieren. Das Formular speichert ihn mit Nextclouds Kryptodienst verschlüsselt. Bei späteren Änderungen bleibt das Token-Feld leer, wenn du den vorhandenen Token beibehalten möchtest. Normale Benutzer können diese Dienstverbindung weder verändern noch den Dienst-Token auslesen.

### 5. Apple und Nextcloud anmelden

Unter **Verbindungen**:

1. Apple-Account und **reguläres Apple-Passwort** eingeben. Apples anwendungsspezifische Passwörter werden von diesem rclone-Backend nicht akzeptiert.
2. Der angezeigten Apple-Abfrage folgen, normalerweise mit einem sechsstelligen Code vom vertrauten Gerät. Wenn angeboten, mit `sms` eine SMS-Abfrage anfordern. Weitere Fragen werden direkt aus dem rclone-Anmeldeablauf angezeigt.
3. In Nextcloud unter **Persönliche Einstellungen → Sicherheit** ein App-Passwort anlegen, beispielsweise `iCloud Drive Bridge`.
4. Tatsächliche Nextcloud-Anmelde-ID und App-Passwort im Nextcloud-Verbindungsformular hinterlegen. Der Anzeigename kann von der Anmelde-ID abweichen, besonders bei OIDC oder LDAP.

Der anfängliche Apple-Anmeldedialog läuft nach 15 Minuten ab; bei Bedarf neu beginnen. Der Apple-Vertrauenstoken gilt laut Dokumentation ungefähr 30 Tage. Plane eine regelmäßige Erneuerung der Apple-Anmeldung ein. In den letzten sieben Tagen des geschätzten Zeitraums erscheint ein Hinweis. Ein früherer Ablauf bleibt möglich.

Bei erweitertem Datenschutz muss **Zugriff auf iCloud-Daten im Web** aktiviert sein. Bestätige zusätzliche Apple-Anfragen auf einem vertrauten Gerät. Die Unterstützung stammt aus dem verwendeten rclone-Backend. Eine erfolgreiche Identitätsprüfung allein beweist noch keinen Dateizugriff; die Bridge prüft vor dem Verbindungsstatus auch die iCloud-Ordnerliste.

### 6. Erstes Ordnerpaar einrichten

1. Einen kleinen Testordner mit wenigen Dateien in iCloud erstellen.
2. In der App **Ordner hinzufügen** wählen.
3. iCloud-Ordner und Nextcloud-Ziel auswählen. Beide müssen existieren; ein Nextcloud-Ziel lässt sich in der Ordnerauswahl erstellen.
4. **Beide Richtungen** wählen, **Beide als Konfliktkopien behalten** und Sicherungsordner aktiviert lassen.
5. Festlegen, welche Seite bei unterschiedlichen vorhandenen Dateien gleichen Namens im Erstabgleich Vorrang hat.
6. Speichern, **Vorschau** starten und das Aktivitätsprotokoll prüfen.
7. Nach erfolgreicher Vorschau **Erstabgleich starten**. Dadurch entstehen Prüfdateien und die ersten bisync-Zustände.
8. Automatische Läufe aktivieren und Wochentage/Uhrzeit oder Intervall festlegen. Bereits aktivierte automatische Zwei-Wege-Jobs werden erst nach erfolgreicher Initialisierung eingeplant.

Prüfe eine neue Datei je Seite, Änderungen je Seite, eine Löschung und gleichzeitige unterschiedliche Änderungen derselben Datei. Kontrolliere Ergebnisse und Sicherungen. Ein fehlgeschlagener echter Lauf pausiert den Job. Bei Zwei-Wege-Jobs folgen Prüfung, neue Vorschau und ausdrücklicher Erstabgleich; ein automatischer Resync nach Fehlern findet nicht statt.

## iCloud in Nextcloud Dateien durchsuchen

Diese Funktion ist unabhängig vom Abgleich. Sie zeigt iCloud mit Lesezugriff und ohne vollständige Nextcloud-Kopie an.

1. Nextclouds eingebaute App **External storage support / Unterstützung für externen Speicher** aktivieren. Beim offiziellen Container:

   ```bash
   docker exec -u www-data NEXTCLOUD_CONTAINER php /var/www/html/occ app:enable files_external
   ```

   Benutzer und Pfad bei LinuxServer oder anderen Installationen anpassen.
2. In **iCloud Drive → Verbindungen** die **Zugangsdaten zur Einbindung anzeigen**.
3. Unter **Persönliche Einstellungen → Externer Speicher** eine **WebDAV**-Einbindung namens `iCloud Live` anlegen.
4. Angezeigte URL, Benutzername und Einbindungs-Passwort kopieren. Bei `http://icloud-bridge:8080/...` bleibt HTTPS abgewählt. Diese Zugangsdaten unterscheiden sich vom Apple-Passwort und vom Dienst-Token.
5. Ist persönlicher externer Speicher deaktiviert, muss der Administrator WebDAV dafür freigeben oder eine auf deinen Benutzer beschränkte administrative Einbindung anlegen.

Die URL verwendet der **Nextcloud-Server**. Browser und Mobilgeräte müssen `icloud-bridge` nicht auflösen können. Veröffentliche den Bridge-Port nicht im Internet. Änderungen direkt in iCloud erscheinen nach ungefähr einer Minute im Gateway, zuzüglich möglicher Nextcloud-Cache-Verzögerung. Vorschaubilder und Indexierung können zusätzliche Dateien in den temporären Cache laden.

Beim Trennen der iCloud-Verbindung werden das Einbindungs-Passwort entfernt und der direkte Prozess beendet. Die erneute Verbindung erzeugt ein neues Passwort; aktualisiere dann vorhandene externe Speicher. Die direkte Ansicht ist schreibgeschützt, damit ihre Schreibzugriffe nicht mit dem täglichen Abgleich kollidieren.

## Einstellungen je Ordnerpaar

| Einstellung | Standard / Bereich | Wirkung |
|---|---|---|
| Richtung | Beide / Import / Export | Nur Beide Richtungen überträgt Löschungen |
| Automatische Läufe | Aus | Manuelle Aktionen bleiben verfügbar; Zwei-Wege-Jobs benötigen Initialisierung |
| Zeitplan | Täglich 03:00 | Manuell, ausgewählte Wochentage oder Intervall |
| Zeitzone | IANA-Zone des Browsers | Je Job gespeichert; Sommer-/Winterzeit wird berücksichtigt |
| Intervall | 1.440 Minuten; 60–10.080 | Wird nur im Intervallmodus verwendet |
| Konflikte | Beide behalten | Alternativ neuere Fassung, iCloud oder Nextcloud bevorzugen; andere Fassung bleibt als Konfliktkopie |
| Erstabgleich-Vorrang | iCloud | Entscheidet vorhandene Unterschiede gleichen Pfads; eindeutige Dateien beider Seiten werden zusammengeführt |
| Löschgrenze | 10 %; 0–50 % | Gilt für bisync; Abbruch bei zu hohem Anteil gelöschter Einträge |
| Sicherungen | Ein | Archiviert ersetzte/gelöschte Fassungen auf demselben Speicher außerhalb des Abgleichordners |
| Leere Ordner | Ein | Fordert Erhaltung an, soweit vom Backend unterstützt |
| Ausschlüsse | `.DS_Store`, `._*`, `~$*` | rclone-Globmuster, eines je Zeile; Prüfdatei bleibt eingeschlossen |
| Übertragungen | 2; 1–8 | Parallele Dateiübertragungen |
| Prüfungen | 4; 1–16 | Parallele Überprüfungen |
| Wiederholungen | 3; 1–10 | Übertragungswiederholungen; kritische bisync-Erholung benötigt weiterhin Prüfung |
| Bandbreite | Unbegrenzt | Leer oder ein einzelner Wert wie `10M` |
| Maximale Laufzeit | 60 Minuten; 5–1.440 | Danach wird ein geordnetes Stoppen angefordert |

Ordnerwurzeln, Richtung, Ausschlüsse und Verhalten leerer Ordner definieren die bisync-Zuordnung. Änderungen daran verwerfen die Initialisierung und stellen das bisherige Zustandsverzeichnis zurück. Zeitplan und Übertragungseinstellungen können ohne Verwerfen der Zustände angepasst werden. Überlappende Ordnerbäume werden auf beiden Seiten mit einem konservativen Vergleich ohne Beachtung der Groß-/Kleinschreibung abgewiesen.

## Konflikte, Löschungen, Sicherungen und Wiederaufnahme

Normale Zwei-Wege-Läufe vergleichen beide Seiten mit den vorherigen erfolgreichen Listen. Eine Löschung auf einer Seite löscht die Datei normalerweise auch auf der anderen. Umbenennen eines großen Ordners kann wie zahlreiche Löschungen und neue Dateien aussehen und die Löschgrenze auslösen. Die Datei `.icloud-bridge-check` liegt absichtlich in beiden Abgleichordnern. Entferne sie nicht; sonst bricht der normale Abgleich ab.

Bei **Beide behalten** entstehen bei unterschiedlichen Änderungen nummerierte Konfliktkopien. Bei einer bevorzugten/neuesten Fassung bleibt der Originalname für den Gewinner bestehen; die andere Fassung bleibt als nummerierte Konfliktkopie erhalten. Dokumentinhalte werden nicht automatisch zusammengeführt.

Der Erstabgleich führt eindeutige Dateien beider Seiten zusammen und löst Unterschiede gleichen Pfads mit dem gewählten Vorrang. Konflikt-Umbenennungsregeln gelten dabei nicht. Aktivierte Sicherungen bewahren ersetzte Fassungen unter folgendem Pfad auf:

```text
iCloud Bridge Backups/JOB_ID/RUN_ID/
```

Archive liegen auf dem jeweiligen Speicher außerhalb des ausgewählten Ordners. Sie werden **nicht automatisch gelöscht**. Prüfe den Speicherbedarf und entferne ältere Fassungen gezielt. Diese Archive ersetzen weder unabhängige Server-Backups noch iCloud-Wiederherstellung.

Stoppen sendet SIGINT an den Übertragungsprozess, damit rclone seine Zustände geordnet sichern kann. Ein nach 90 Sekunden weiterhin laufender Prozess wird beendet. Ein Neustart markiert offene Läufe als unterbrochen und pausiert die betroffenen Jobs; Zustände bleiben erhalten. Prüfe Dateien und Protokolle vor einer neuen Vorschau/Initialisierung. `--resync` gehört nicht in jeden wiederkehrenden Lauf.

## Klassische Nextcloud ohne Docker

Die PHP-App funktioniert auch in einer klassischen Installation. Kopiere sie in das konfigurierte beschreibbare App-Verzeichnis und aktiviere sie als PHP-/Webbenutzer. Ihr Ordnername muss `icloud_drive` lauten:

```bash
sudo cp -a nextcloud/icloud_drive /var/www/nextcloud/custom_apps/
sudo chown -R www-data:www-data /var/www/nextcloud/custom_apps/icloud_drive
sudo -u www-data php /var/www/nextcloud/occ app:enable icloud_drive
```

Passe Pfade und Benutzer an. Damit der Docker-Dienst nur auf dem Host erreichbar ist, ergänze eine Compose-Override-Datei:

```yaml
services:
  icloud-bridge:
    ports:
      - "127.0.0.1:18080:8080"
```

Die interne URL im Administrationsformular lautet dann `http://127.0.0.1:18080`. Die Bridge benötigt weiterhin ein Netz mit ausgehendem Zugriff; erstelle dafür ein eigenes Netz und setze `NEXTCLOUD_NETWORK`. Bei Nextcloud auf einem anderen Rechner verwende eine private Schnittstelle mit TLS, Firewall-Beschränkung und passender Route. Die private JSON-API bleibt unveröffentlicht.

## Aktualisierung, Backup und Entfernen

```bash
cd /mnt/docker/compose/Nextcloud-iCloudDrive
git pull --ff-only
docker compose up -d --build
bash scripts/install-nextcloud-app.sh NEXTCLOUD_CONTAINER
```

Aktualisiere außerhalb aktiver Übertragungen. Bewahre `.env`, Schlüssel und das Volume `bridge_data` auf. `docker compose down` erhält benannte Volumes; `docker compose down -v` löscht Zustände und Zugangsdaten. Erstelle konsistente Sicherungen bei gestoppter Bridge, einschließlich Volume und Schlüssel. Sichere synchronisierte Dateien zusätzlich mit dem Nextcloud-/Server-Backup.

Das Entfernen eines Jobs löscht seine Zuordnung und sein aktives Zustandsverzeichnis; Dateien und Archive bleiben bestehen. Das Trennen eines Kontos entfernt dessen rclone-Verbindung und pausiert Jobs. Bei Nextcloud-Benutzerlöschung versucht die App, dessen Bridge-Konto zu entfernen. War die Bridge nicht erreichbar, wird die fehlgeschlagene Bereinigung protokolliert. Ein Betreiber kann nacharbeiten:

```bash
bash scripts/purge-user.sh EXAKTE_NEXTCLOUD_BENUTZER_ID
```

Das stoppt aktive Übertragungen und entfernt Bridge-Zugangsdaten, Zuordnungen und Laufdatensätze. Dateien und Archive der Clouds werden nicht gelöscht. Zurückgestellte Zustandsverzeichnisse können noch alte Dateinamen enthalten und benötigen eine Aufbewahrungsregel. Zur Deinstallation App deaktivieren/entfernen, direkte externe Speicher entfernen und Bridge stoppen. Entscheide ausdrücklich, ob ihr Datenvolume erhalten bleiben soll.

## Fehlerbehebung

| Problem | Prüfen / beheben |
|---|---|
| Bridge nicht erreichbar | Gemeinsames Docker-Netz, `http://icloud-bridge:8080`, Dienst-Token und Containerzustand |
| Schlüsseldatei nicht lesbar | `sudo chown -R 10001:10001 secrets`; beide Dateien müssen existieren |
| Konfiguration nicht entschlüsselbar | Ursprünglichen Schlüssel und passendes Volume wiederherstellen; keinen Ersatzschlüssel erzeugen |
| Nextcloud liefert 401 oder HTML | Anmelde-ID/App-Passwort prüfen; WebDAV darf nicht hinter einer interaktiven SSO-Seite hängen |
| Apple-Bestätigung bleibt offen | Gerätefreigabe bestätigen, sämtliche Fragen abschließen oder abgelaufene Anmeldung neu beginnen |
| PCS-Cookies fehlen / ADP-Fehler | iCloud-Webzugriff aktivieren, Apple-Freigabe bestätigen und Anmeldung erneuern |
| Erstabgleich-Schaltfläche deaktiviert | Zuordnung speichern, erfolgreiche Vorschau ausführen und danach initialisieren |
| Ordner fehlt / Liste fehlerhaft | Beide Wurzeln müssen existieren und erreichbar sein; Nextcloud-Ziel im Picker anlegen |
| Job nach Fehler pausiert | Aktivität und Dateien prüfen, gegebenenfalls neu anmelden, dann Vorschau und bewusster Erstabgleich |
| Löschgrenze nach Umbenennung | Geplante Änderungen prüfen; Grenze für diesen Job gezielt ändern und über die Wiederaufnahme erneut starten |
| Externer Speicher rot | Einbindungs-Zugangsdaten, Erreichbarkeit durch Nextcloud, Apple-Anmeldung und Speicherfreigabe prüfen |
| Nextcloud-Version abgewiesen | Metadaten akzeptieren 30–35; Versionsprüfung nicht ohne Installationstest umgehen |

Hilfreiche Befehle:

```bash
docker compose ps
docker compose logs --tail=100 icloud-bridge
docker compose exec -T icloud-bridge python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
```

Übertragungsdetails findest du unter **Aktivität → Details**. Der Container-Log lässt Anfragetexte und Zugangsdaten absichtlich aus. Teile keine `rclone.conf`, SQLite-Datenbank, Schlüsseldateien, kompletten Anmeldeantworten oder ungeprüften Dateinamen/Logs.

## Grenzen und Sicherheitsmodell

- Apples Webschnittstellen können sich ändern. Eine App-Oberfläche macht das experimentelle Backend nicht zu einer garantiert unbeaufsichtigten Integration.
- Das iCloud-Backend liefert keine Dateiprüfsummen. Normal verglichen werden Größe und Änderungszeit; Änderungen bei unveränderten Werten können unentdeckt bleiben.
- Für geteilte iCloud-Ordner, insbesondere Unterordner, gibt es gemeldete Backend-Probleme. Vor automatischem Abgleich separat testen.
- iCloud-Freigaberechte, Zusammenarbeit und Apple-Kontometadaten werden nicht als Nextcloud-Freigaben synchronisiert.
- Kopien und Archive belegen Speicher auf beiden Diensten. Der direkte VFS-Cache kann bei geöffneten Dateien sein eingestelltes Größenlimit vorübergehend überschreiten.
- Die JSON-API gehört ins vertrauenswürdige interne Netz hinter Nextcloud. Ihr Dienst-Token ist eine Administratorberechtigung für sämtliche Bridge-Benutzer. Ein kompromittierter Host, Bridge-Dienst oder Nextcloud-Administrator kann Zugangsdaten erreichen.
- rclones verschleierte Passwortdarstellung allein ist reversibel. Die Bridge verschlüsselt deshalb die gesamte rclone-Konfiguration sowie SQLite-Zugangsdatenfelder mit einem separat gespeicherten Schlüssel. Dateinamen, Konfiguration, Status und bereinigte Protokolle bleiben Betriebsmetadaten.
- Direkte Einbindungen erhalten ein eigenes Passwort je Benutzer. Das Gateway erlaubt nur OPTIONS, PROPFIND, GET und HEAD für dessen eigenen Remote.
- Über die App werden keine freien Shell-Befehle, rclone-RC-Endpunkte, Remote-Definitionen oder zusätzlichen rclone-Flags angenommen. Der Docker-Socket wird nicht eingebunden.
- Die Benutzerkennung stammt aus der serverseitigen Anmeldung. Administrationsrouten verlangen Administratorrechte; andere OCS-Routen behalten Nextclouds Anmeldung und CSRF-/OCS-Schutz bei.

## Entwicklung und Prüfungen

Auf einem Host mit installiertem `cryptography`:

```bash
python3 -m pip install -r worker/requirements.txt
PYTHONPATH=worker python3 -m unittest discover -s worker/tests -v
```

Tests mit echtem rclone werden übersprungen, wenn dessen Binärdatei fehlt. Im gebauten Bridge-Image laufen sie mit:

```bash
docker build -t icloud-bridge:test worker
docker run --rm -v "$PWD/worker/tests:/tests:ro" icloud-bridge:test python3 -m unittest discover -s /tests -v
```

Optionale Browserprüfungen verwenden die echte ausgelieferte Oberfläche mit Testdaten. Geprüft werden deutsche/englische Formulare, gespeicherte Einstellungen, Einbindungsdaten und Überbreite auf Mobilgeräten; Apple wird dabei nicht angemeldet:

```bash
npm install --ignore-scripts
npx playwright install chromium
npm run test:ui
```

Der Nextcloud-Smoke-Test installiert die App in einem temporären offiziellen Container, prüft OCS-Zugriff als normaler Benutzer und die Bridge-Verbindung zu Nextcloud-WebDAV:

```bash
NC_VERSION=30 bash tests/nextcloud-smoke.sh
```

GitHub Actions führt Worker-/rclone-Tests, PHP-Syntaxprüfungen, Browserprüfungen und eine Nextcloud-30/35-Matrix aus. Echte Apple-Anmeldung und iCloud-Übertragungen bleiben außerhalb von CI. Prüfe den aktuellen Actions-Status, bevor du eine Fassung als validiert behandelst. `python3 scripts/package.py` erzeugt ein installierbares App-Archiv. Es enthält nur die App `icloud_drive`, keinen Docker-Dienst und keine Schlüssel. Die App ist unsigniert und wird manuell installiert; sie ist keine App-Store-Veröffentlichung.

## Quellen und Lizenz

- [rclone-iCloud-Backend](https://rclone.org/iclouddrive/)
- [rclone-Unterstützungsstufen](https://rclone.org/tiers/)
- [rclone-bisync-Handbuch](https://rclone.org/bisync/)
- [rclone-Nextcloud-/WebDAV-Backend](https://rclone.org/webdav/)
- [Nextcloud-WebDAV-Zugriff](https://docs.nextcloud.com/server/latest/user_manual/en/files/access_webdav.html)
- [Nextcloud-WebDAV als externer Speicher](https://docs.nextcloud.com/server/latest/admin_manual/configuration_files/external_storage/webdav.html)

Projektcode: AGPL-3.0-or-later, siehe [LICENSE](LICENSE). rclone und Containerkomponenten behalten ihre eigenen Lizenzen. Apple und Nextcloud werden zur Beschreibung der Kompatibilität genannt.
