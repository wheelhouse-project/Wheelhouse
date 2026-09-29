// The explanation window the website shows before the Wheelhouse Assistant
// opens (wh-assistant-gemini-notebook). It says the same words as the
// "Ask the Wheelhouse Assistant" window in Wheelhouse: render_site.py
// replaces the TEXT placeholder below with the text of
// services/wheelhouse/help_explainer_window.py when it builds the site, so
// the two windows cannot drift apart. Edit the text there, never here.
//
// Every Assistant link stays a plain link, so a browser without JavaScript
// (or without the dialog element) opens the notebook directly. With
// JavaScript, a plain left click on an Assistant link opens this window instead. The
// Assistant button then follows the link; Cancel and the Escape key close
// the window and open nothing.
//
// The "Do not show this again" choice is kept in this browser's local
// storage only. Clearing site data, a private window, or blocked
// storage forgets it, so the window appears again; every storage access is
// inside try/catch for that reason.
const TEXT = {
  "address": "https://notebook.google.com/notebook/da51a404-67ec-4804-9ebe-83605df3e9cf/preview",
  "title": "Ask the Wheelhouse Assistant",
  "paragraphs": [
    "The Wheelhouse Assistant answers questions about Wheelhouse. The Wheelhouse Assistant runs on Google's Gemini Notebook, so you must sign in with a Google Account.",
    "The account is free, and Google does not ask for a credit card. You can use an email address you already have; a Gmail address is not necessary.",
    "To create an account, click \"Create account\" on the Google sign-in page.",
    "If you are signed in with a work or school account and the Assistant does not open, sign in with a personal account instead.",
    "Then type or dictate your question in plain words."
  ],
  "doNotShowAgain": "Do not show this again",
  "assistantButton": "Assistant",
  "cancelButton": "Cancel"
};

const STORAGE_KEY = 'wheelhouse-assistant-explained';
const HIDE_VALUE = 'hide';

function windowIsHidden() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === HIDE_VALUE;
  } catch (error) {
    return false;
  }
}

function hideWindowFromNowOn() {
  try {
    window.localStorage.setItem(STORAGE_KEY, HIDE_VALUE);
  } catch (error) {
    // Storage is blocked; the window appears again next time.
  }
}

// Open the link the same way the browser would have: a link with a target
// (the site's links use _blank) opens in a new tab without an opener, as
// rel="noopener" does; a link without one opens in this tab.
function followLink(link) {
  const target = link.getAttribute('target');
  if (target && target !== '_self') {
    window.open(link.href, target, 'noopener');
  } else {
    window.location.assign(link.href);
  }
}

// undefined until the first click; null when the browser has no dialog.
let parts;
let pendingLink = null;

function buildWindow() {
  const dialog = document.createElement('dialog');
  if (typeof dialog.showModal !== 'function') {
    return null;
  }
  dialog.className = 'assistant-window';
  dialog.setAttribute('aria-labelledby', 'assistant-window-title');

  const heading = document.createElement('h2');
  heading.id = 'assistant-window-title';
  heading.textContent = TEXT.title;
  dialog.appendChild(heading);

  for (const paragraph of TEXT.paragraphs) {
    const element = document.createElement('p');
    element.textContent = paragraph;
    dialog.appendChild(element);
  }

  const label = document.createElement('label');
  label.className = 'assistant-window-check';
  const box = document.createElement('input');
  box.type = 'checkbox';
  label.appendChild(box);
  label.appendChild(document.createTextNode(' ' + TEXT.doNotShowAgain));
  dialog.appendChild(label);

  // The same order as the Wheelhouse window: Cancel, then Assistant.
  const buttons = document.createElement('div');
  buttons.className = 'assistant-window-buttons';
  const cancelButton = document.createElement('button');
  cancelButton.type = 'button';
  cancelButton.className = 'button assistant-window-cancel';
  cancelButton.textContent = TEXT.cancelButton;
  const assistantButton = document.createElement('button');
  assistantButton.type = 'button';
  assistantButton.className = 'button';
  assistantButton.textContent = TEXT.assistantButton;
  assistantButton.autofocus = true;
  buttons.appendChild(cancelButton);
  buttons.appendChild(assistantButton);
  dialog.appendChild(buttons);

  cancelButton.addEventListener('click', () => {
    dialog.close();
  });
  assistantButton.addEventListener('click', () => {
    if (box.checked) {
      hideWindowFromNowOn();
    }
    dialog.close();
    if (pendingLink) {
      followLink(pendingLink);
    }
  });

  document.body.appendChild(dialog);
  return { dialog, box, assistantButton };
}

document.addEventListener('click', (event) => {
  // Only a plain left click shows the window. Visitors use a middle click or
  // a click with Ctrl, Shift, Alt, or Meta held to open the link in a new
  // tab or window, so those follow the link as the browser would. A link
  // activated with the Enter key arrives as a plain left click.
  if (event.button !== 0 || event.ctrlKey || event.shiftKey || event.altKey || event.metaKey) {
    return;
  }
  const start = event.target;
  if (!start || typeof start.closest !== 'function') {
    return;
  }
  const link = start.closest('a[href]');
  if (!link || link.getAttribute('href') !== TEXT.address) {
    return;
  }
  if (windowIsHidden()) {
    return;
  }
  if (parts === undefined) {
    parts = buildWindow();
  }
  if (!parts) {
    return;
  }
  event.preventDefault();
  pendingLink = link;
  parts.box.checked = false;
  parts.dialog.showModal();
  parts.assistantButton.focus();
});
