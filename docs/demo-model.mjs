// This is an illustration of documented commands, not speech recognition.
export const initialState = () => ({ text: '', selected: false, history: [] });

export function applyCommand(state, command) {
  if (command === 'reset') return initialState();
  if (command === 'select') return { ...state, selected: state.text.length > 0 };
  if (command === 'undo') {
    if (!state.history.length) return state;
    return { text: state.history.at(-1), selected: false, history: state.history.slice(0, -1) };
  }
  if (command !== 'dictate' && command !== 'newline') return state;
  const prefix = state.selected ? '' : state.text;
  const addition = command === 'newline' ? '\n' : (prefix && !prefix.endsWith('\n') ? ' ' : '') + 'Hello world.';
  return { text: prefix + addition, selected: false, history: [...state.history, state.text] };
}
