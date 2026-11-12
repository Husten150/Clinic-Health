// Firebase Authentication for Clinic Health. Uses the modular SDK from Google's CDN.
import { initializeApp } from "https://www.gstatic.com/firebasejs/11.10.0/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithPopup, getIdToken, setPersistence, browserLocalPersistence } from "https://www.gstatic.com/firebasejs/11.10.0/firebase-auth.js";

const firebaseConfig = {
  apiKey: "AIzaSyAnk8ZHpVDoJ9b-VPZCP-zqOmqT5rbiSB0",
  authDomain: "clinic-health-e8414.firebaseapp.com",
  projectId: "clinic-health-e8414",
  storageBucket: "clinic-health-e8414.firebasestorage.app",
  messagingSenderId: "698550863941",
  appId: "1:1a6f60b2c34e263abbfb2c",
  measurementId: "G-SR61FRBQ68"
};

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const provider = new GoogleAuthProvider();
provider.setCustomParameters({ prompt: "select_account" });
const byId = (id) => document.getElementById(id);
function showError(message) {
  const box = byId("form-message");
  if (box) { box.textContent = message; box.className = "form-message error"; }
}
async function signIn(mode) {
  const button = byId(mode === "signup" ? "firebase-google-signup" : "firebase-google-signin");
  if (!button) return;
  button.disabled = true;
  try {
    let dateOfBirth;
    if (mode === "signup") {
      dateOfBirth = byId("dob")?.value;
      if (!dateOfBirth) { showError("Enter your date of birth before continuing with Google."); return; }
    }
    await setPersistence(auth, browserLocalPersistence);
    const result = await signInWithPopup(auth, provider);
    const idToken = await getIdToken(result.user, true);
    const response = await fetch("/api/auth/firebase", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id_token: idToken, ...(dateOfBirth ? { date_of_birth: dateOfBirth } : {}) })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Firebase authentication failed.");
    sessionStorage.setItem("clinicPatientToken", data.access_token);
    sessionStorage.setItem("clinicPatient", JSON.stringify(data.patient));
    window.location.assign("/appointments");
  } catch (error) {
    const code = error?.code || "";
    showError(code === "auth/unauthorized-domain"
      ? "This website domain is not authorized in Firebase Authentication settings."
      : code === "auth/popup-closed-by-user"
        ? "Google sign-in was cancelled. Please try again."
        : error.message || "Could not sign in with Google.");
  } finally { button.disabled = false; }
}
document.addEventListener("DOMContentLoaded", () => {
  byId("firebase-google-signup")?.addEventListener("click", () => signIn("signup"));
  byId("firebase-google-signin")?.addEventListener("click", () => signIn("signin"));
});
