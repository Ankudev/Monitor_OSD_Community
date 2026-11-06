"use strict";
(() => {
  const start = document.getElementById("pushSubscribe");
  const notice = document.getElementById("pushStatus");
  const device = document.getElementById("pushDevice");
  let initialized = false, sdk, initialization;
  start.addEventListener("click", async () => {
    start.disabled = true;
    notice.textContent = "Przygotowywanie subskrypcji…";
    try {
      const response = await fetch("/api/admin/push/config", {cache: "no-store", signal: AbortSignal.timeout(10000)});
      const config = await response.json();
      if (!response.ok) {
        notice.textContent = config.error || "Konfiguracja powiadomień jest niedostępna. Sprawdź sesję administratora.";
        return;
      }
      if (!initialized) {
        await new Promise((resolve, reject) => {
          window.OneSignalDeferred = window.OneSignalDeferred || [];
          window.OneSignalDeferred.push(async loaded => {
            sdk = loaded;
            try {
              initialization = initialization || sdk.init({appId: config.app_id, autoResubscribe: false,
                serviceWorkerPath: "/osd-push/OneSignalSDKWorker.js",
                serviceWorkerParam: {scope: "/osd-push/"}, notifyButton: {enable: false},
                welcomeNotification: {disable: true}});
              await initialization;
              resolve();
            } catch (error) { reject(error); }
          });
          const script = document.createElement("script");
          script.src = "https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js";
          script.onerror = reject;
          document.head.append(script);
        });
        initialized = true;
        sdk.User.PushSubscription.addEventListener("change", show);
      }
      await sdk.Notifications.requestPermission();
      if (sdk.Notifications.permission) await sdk.User.PushSubscription.optIn();
      show();
    } catch {
      notice.textContent = "Nie udało się włączyć powiadomień. Sprawdź, czy sesja administratora jest aktywna i czy przeglądarka zezwala na powiadomienia. Użyj aktualnej wersji przeglądarki Chrome, Edge lub Firefox poza trybem prywatnym.";
    } finally { start.disabled = false; }
  });
  function show() {
    const id = sdk.User.PushSubscription.id;
    device.textContent = id || "Jeszcze nie nadano";
    notice.textContent = sdk.User.PushSubscription.optedIn && id
      ? "Subskrypcja aktywna. Odbiorca musi być wskazany w konfiguracji alarmów."
      : "Brak aktywnej subskrypcji. Sprawdź zgodę na powiadomienia dla tej witryny.";
  }
})();
