chrome.webRequest.onAuthRequired.addListener(
  (details, callbackFn) => {
    callbackFn({
      authCredentials: {
        username: "PROXY_USER",
        password: "PROXY_PASS"
      }
    });
  },
  { urls: ["<all_urls>"] },
  ["asyncBlocking"]
);
