# Food Scanner per Android

Questa è la confezione Android di Food Scanner: un APK autonomo, senza Termux e
senza server da avviare a mano. L'app contiene:

- la stessa UI web della versione desktop;
- il motore Python di scoring;
- la tabella dei 546 additivi;
- la demo offline;
- il client Open Food Facts.

Tutto gira **dentro l'app**: la WebView apre `127.0.0.1` e il server Python ascolta
solo sul loopback del telefono.

## Requisiti

- Android 17 (API 37) o superiore;
- telefono **arm64**;
- connessione Internet per cercare prodotti su Open Food Facts;
- nessun account, nessuna chiave e nessun Termux.

L'APK è compilato con `compileSdk 37.0`, `targetSdk 37`, `minSdk 26`.

## Installazione

1. Apri la pagina **Releases** del repository.
2. Scarica `FoodScanner-0.3-beta.apk`.
3. Apri il file dal telefono.
4. Se Android chiede il permesso, abilita **Installa app sconosciute** per
   Chrome o per il file manager.
5. Se compare l'avviso di Play Protect, scegli **Installa comunque**: l'APK è
   firmato con la chiave di debug, non è pubblicato sul Play Store.
6. Apri **Food Scanner** e attendi il primo avvio: gli asset Python vengono
   copiati nella memoria privata dell'app.

Non serve installare Python, Termux o altro.

## Come si usa

- **Barcode**: digita il codice e premi **Analizza**.
- **Demo offline**: prova un prodotto senza rete.
- **Fotocamera**: il pulsante apre la fotocamera o la galleria. La lettura
  automatica del barcode dipende dal supporto `BarcodeDetector` della WebView;
  se non è disponibile, inserisci il codice a mano.
- **Risultato**: score, componenti, additivi, nutrienti e spiegazioni.

La ricerca di un prodotto nuovo richiede Internet e Open Food Facts. La demo e
lo scoring di un JSON già scaricato funzionano offline.

## Cosa non è incluso

- L'invio delle foto a Open Food Facts non è configurato nell'APK: non ci sono
  credenziali dentro il pacchetto. Il pulsante **+ Aggiungi foto** resta quindi
  disabilitato lato server.
- Non ci sono analytics, pubblicità o account.
- La cache dei prodotti è nella memoria privata dell'app e si cancella
  disinstallandola.

## Android 17 e installazione da APK

Android 17 è API 37. L'APK è già compilato e targettizzato per API 37.

Per installare un APK scaricato da GitHub serve il normale consenso **Installa
app sconosciute**. La verifica sviluppatore di Google è in vigore dal 30
settembre 2026 solo per installazioni da alcuni store in Brasile, Indonesia,
Singapore e Thailandia; il rollout globale è previsto nel 2027. Se in futuro
Android mostra il flusso avanzato per sviluppatori non verificati, puoi
consentire l'installazione dalle opzioni sviluppatore oppure usare `adb install`
(che resta disponibile).

## Build

La build automatica è nel workflow `.github/workflows/android-apk.yml`.

Requisiti locali:

- JDK 17;
- Android SDK 37 e Build Tools 36.0.0;
- Gradle 9.6.0.

```bash
cd android
gradle :app:assembleDebug
```

L'APK esce in:

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

Il task Gradle `stageFoodScannerAssets` copia automaticamente `engine/`, `ui/` e
`data/` negli asset dell'APK: non ci sono copie duplicate da mantenere a mano.

## Struttura

```text
android/
  app/src/main/java/com/foodscanner/app/MainActivity.java
  app/src/main/python/bootstrap.py
  app/src/main/res/                  layout, tema, icona, FileProvider
  app/build.gradle.kts               Chaquopy, SDK 37, staging asset
  build.gradle.kts
  settings.gradle.kts
.github/workflows/android-apk.yml    build APK su GitHub Actions
```

## Firma e aggiornamenti

L'APK è firmato con la chiave di debug generata dalla build. Va bene per
un'installazione personale e per il sideload, non per il Play Store. Se in
futuro cambi la firma, Android può rifiutare l'aggiornamento sopra l'app
esistente: in quel caso disinstalla la vecchia versione e installa la nuova.
