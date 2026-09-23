import { initialState, applyCommand } from './demo-model.mjs';

const controls = document.querySelector('.demo-controls');
const output = document.querySelector('#example-text');
const spoken = document.querySelector('#spoken-command');
const explanation = document.querySelector('#example-explanation');
const announcement = document.querySelector('#demo-announcement');
const labels = {
  dictate: ['“hello world period”', 'Your words appear, with the punctuation you asked for.', '01 / 04'],
  newline: ['“new line”', 'The cursor moves to the next line.', '02 / 04'],
  undo: ['“undo”', 'Your last text change is undone.', '03 / 04'],
  select: ['“select all”', 'The text is selected, ready for your next command.', '04 / 04'],
  reset: ['Your turn.', 'Choose “Say hello” to put some words on the page.', '00 / 04'],
};
let state = applyCommand(initialState(), 'dictate');

controls.hidden = false;
controls.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-command]');
  if (!button || !controls.contains(button)) return;
  const command = button.dataset.command;
  if (!Object.hasOwn(labels, command)) return;
  const previous = state;
  state = applyCommand(state, command);
  const [phrase, description, number] = labels[command];
  let message = description;
  if (command === 'undo' && !previous.history.length) message = 'There’s no text change to undo yet.';
  if (command === 'select' && !state.text) message = 'There’s no text to select yet. Try “Say hello”.';
  spoken.textContent = phrase;
  output.textContent = state.text;
  output.classList.toggle('selected-text', state.selected);
  explanation.textContent = message;
  document.querySelector('.demo-number').textContent = number;
  announcement.textContent = `${phrase} ${message} ${state.text ? `Document: ${state.text.replaceAll('\n', ' [new line] ')}` : 'The document is empty.'}`;
});
