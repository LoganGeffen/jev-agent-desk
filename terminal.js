const terminalPalette = [
  '#000000', '#cd0000', '#00cd00', '#cdcd00', '#0000ee', '#cd00cd', '#00cdcd', '#e5e5e5',
  '#7f7f7f', '#ff0000', '#00ff00', '#ffff00', '#5c5cff', '#ff00ff', '#00ffff', '#ffffff'
];

function terminalColor(index) {
  if (index < 16) return terminalPalette[index];
  if (index >= 232) {
    const gray = 8 + (index - 232) * 10;
    return `rgb(${gray}, ${gray}, ${gray})`;
  }
  const levels = [0, 95, 135, 175, 215, 255];
  const value = index - 16;
  return `rgb(${levels[Math.floor(value / 36)]}, ${levels[Math.floor(value / 6) % 6]}, ${levels[value % 6]})`;
}

function terminalRuns(screen) {
  const runs = [];
  let style = {};
  const parts = screen.split(/(\x1b\[[0-9;:]*m)/);
  for (const part of parts) {
    if (!part.startsWith('\x1b[')) {
      if (part) runs.push({text: part, ...style});
      continue;
    }
    const codes = part.slice(2, -1).split(/[;:]/).map(Number);
    for (let i = 0; i < codes.length; i++) {
      const code = codes[i];
      if (code === 0) style = {};
      else if (code === 1) style.bold = true;
      else if (code === 2) style.dim = true;
      else if (code === 3) style.italic = true;
      else if (code === 4) style.underline = true;
      else if (code === 7) style.inverse = true;
      else if (code === 9) style.strike = true;
      else if (code === 22) { delete style.bold; delete style.dim; }
      else if (code === 23) delete style.italic;
      else if (code === 24) delete style.underline;
      else if (code === 27) delete style.inverse;
      else if (code === 29) delete style.strike;
      else if (code === 39) delete style.foreground;
      else if (code === 49) delete style.background;
      else if (code >= 30 && code <= 37) style.foreground = terminalColor(code - 30);
      else if (code >= 40 && code <= 47) style.background = terminalColor(code - 40);
      else if (code >= 90 && code <= 97) style.foreground = terminalColor(code - 90 + 8);
      else if (code >= 100 && code <= 107) style.background = terminalColor(code - 100 + 8);
      else if (code === 38 || code === 48) {
        const property = code === 38 ? 'foreground' : 'background';
        const mode = codes[++i];
        if (mode === 5) style[property] = terminalColor(codes[++i]);
        else if (mode === 2) {
          style[property] = `rgb(${codes[i + 1]}, ${codes[i + 2]}, ${codes[i + 3]})`;
          i += 3;
        }
      }
    }
  }
  return runs;
}

function renderTerminal(element, screen) {
  if (element.terminalScreen === screen) return false;
  element.terminalScreen = screen;
  const fragment = document.createDocumentFragment();
  for (const run of terminalRuns(screen)) {
    const span = document.createElement('span');
    span.textContent = run.text;
    const foreground = run.foreground || 'var(--terminal-foreground)';
    const background = run.background || 'var(--terminal-background)';
    span.style.color = run.inverse ? background : foreground;
    span.style.backgroundColor = run.inverse ? foreground : background;
    if (run.inverse) span.classList.add('terminal-inverse');
    if (run.bold) span.style.fontWeight = 'bold';
    if (run.dim) span.style.opacity = '.65';
    if (run.italic) span.style.fontStyle = 'italic';
    span.style.textDecoration = [run.underline && 'underline', run.strike && 'line-through'].filter(Boolean).join(' ');
    fragment.append(span);
  }
  element.replaceChildren(fragment);
  return true;
}

function fitTerminal(element) {
  if (!element.clientWidth) return;
  const previousSize = parseFloat(getComputedStyle(element).fontSize);
  const scrollLine = element.scrollTop / previousSize;
  const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 36;
  element.style.fontSize = '';
  const style = getComputedStyle(element);
  const baseSize = parseFloat(style.fontSize);
  const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  const available = element.clientWidth - padding;
  const width = element.scrollWidth - padding;
  const size = width > available ? Math.floor(baseSize * (available - 1) / width * 100) / 100 : baseSize;
  if (size > 0) element.style.fontSize = size + 'px';
  element.scrollLeft = 0;
  element.scrollTop = nearBottom ? element.scrollHeight : scrollLine * size;
}
