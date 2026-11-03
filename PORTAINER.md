# Ohne Registry direkt in Portainer bauen

Damit wird das Image auf dem ZimaOS-/Portainer-Rechner gebaut. GitHub, GHCR und ein Architektur-Export vom Mac sind nicht nötig.

> [!IMPORTANT]
> Diese Anleitung und [`portainer-stack.yml`](portainer-stack.yml) verwenden ein **lokal gebautes Image** und deshalb `pull_policy: never`. Für das veröffentlichte GHCR-Image stattdessen [`portainer-ghcr-stack.yml`](portainer-ghcr-stack.yml) verwenden; diese Variante zieht `ghcr.io/vdite/asin-product-watch:latest` automatisch.

## 1. Image bauen

1. In Portainer das ZimaOS-Docker-Environment öffnen.
2. **Images → Build a new image** öffnen.
3. Als Namen `asin-product-watch:local` eintragen.
4. Als Build-Methode **Web editor** auswählen.
5. Den Inhalt von [`Dockerfile.portainer`](Dockerfile.portainer) in den Editor kopieren.
6. Unter **Select files** gleichzeitig diese vier Dateien auswählen:
   - `app/asin_product_watch.py`
   - `app/icon.svg`
   - `LICENSE`
   - `THIRD_PARTY_NOTICES.md`
7. **Build the image** wählen.
8. Im Output muss der Build erfolgreich enden. Unter **Images** sollte danach `asin-product-watch:local` erscheinen.

Portainer legt die über **Select files** gewählten Dateien mit ihrem Dateinamen in den Build-Kontext. Deshalb verwendet das Portainer-Dockerfile absichtlich `COPY asin_product_watch.py icon.svg LICENSE THIRD_PARTY_NOTICES.md ./` statt des Verzeichnisses `app/`.

## 2. Stack anlegen

1. **Stacks → Add stack** öffnen und `asin-product-watch` als Namen eintragen.
2. **Web editor** auswählen und den Inhalt von [`portainer-stack.yml`](portainer-stack.yml) einfügen.
3. Unter **Environment variables** mindestens folgende Variablen ergänzen:

   - `AMAZON_CREDENTIAL_ID`
   - `AMAZON_CREDENTIAL_SECRET`
   - `AMAZON_CREDENTIAL_VERSION` = `3.2`
   - `AMAZON_PARTNER_TAG`
   - `AMAZON_MARKETPLACE` = `www.amazon.de`
   - `PUSHOVER_APP_TOKEN`
   - `PUSHOVER_USER_KEY`

4. **Deploy the stack** wählen.
5. Die Weboberfläche über `http://<zima-ip>:8644` öffnen.

`pull_policy: never` verhindert, dass Docker das lokale Image in einer öffentlichen Registry sucht. Die App-Daten bleiben unter `/DATA/AppData/asin-product-watch` erhalten.

## Update ohne Registry

1. Das Image unter **Images → Build a new image** erneut mit demselben Namen `asin-product-watch:local` bauen.
2. Danach den Stack öffnen und **Update the stack** wählen. Falls Portainer den bestehenden Container nicht ersetzt, zusätzlich **Re-pull image** deaktiviert lassen und **Force redeployment** wählen.
