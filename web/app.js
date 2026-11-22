const form = document.querySelector('#ask-form');
const question = document.querySelector('#question');
const submit = document.querySelector('#submit');
const status = document.querySelector('#progress');
const dialog = document.querySelector('#source-dialog');
let openRequest = 0;

async function json(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

async function showSource(id, quote = '') {
  const request = ++openRequest;
  const title = document.querySelector('#dialog-title');
  const documentName = document.querySelector('#dialog-document');
  const text = document.querySelector('#dialog-text');
  documentName.textContent = '';
  title.textContent = 'Source passage';
  text.textContent = 'Loading source…';
  if (!dialog.open) dialog.showModal();
  try {
    const path = id.split('/').map(encodeURIComponent).join('/');
    const source = await json('/api/sources/' + path);
    if (request !== openRequest) return;
    documentName.textContent = source.document;
    title.textContent = source.section;
    const start = quote ? source.text.indexOf(quote) : -1;
    if (start < 0) {
      text.textContent = source.text;
      return;
    }
    const mark = document.createElement('mark');
    mark.textContent = quote;
    text.replaceChildren(source.text.slice(0, start), mark, source.text.slice(start + quote.length));
  } catch {
    if (request === openRequest) text.textContent = 'Could not load the source. Try again.';
  }
}

function sourceLink(citation, label) {
  const link = document.createElement('a');
  link.href = citation.url;
  link.textContent = label;
  link.addEventListener('click', event => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    void showSource(citation.id, citation.quote);
  });
  return link;
}

function render(result) {
  const answer = document.querySelector('#answer');
  const sources = document.querySelector('#sources');
  answer.replaceChildren();
  sources.replaceChildren();
  for (const [index, citation] of result.citations.entries()) {
    const number = index + 1;
    const paragraph = document.createElement('p');
    paragraph.append(citation.quote, sourceLink(citation, `[${number}]`));
    answer.append(paragraph);
    const item = document.createElement('li');
    const section = document.createElement('small');
    section.textContent = citation.document;
    const quote = document.createElement('blockquote');
    quote.textContent = citation.quote;
    item.append(sourceLink(citation, `${number}. ${citation.section}`), section, quote);
    sources.append(item);
  }
  if (!result.citations.length) {
    const paragraph = document.createElement('p');
    paragraph.textContent = result.answer;
    answer.append(paragraph);
  }
  document.querySelector('#mode').textContent = {
    ollama: 'Model-selected excerpts',
    extractive: 'Indexed excerpts',
  }[result.mode] ?? 'Insufficient evidence';
  document.querySelector('#count').textContent = result.citations.length;
  document.querySelector('#source-panel').hidden = !result.citations.length;
  document.querySelector('#result').hidden = false;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submit.disabled) return;
  const text = question.value.trim();
  if (text.length < 3) {
    status.textContent = 'Enter a question with at least three characters.';
    question.focus();
    return;
  }
  submit.disabled = true;
  document.querySelector('#result').hidden = true;
  status.textContent = 'Searching the knowledge base…';
  try {
    render(await json('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: text }),
    }));
    status.textContent = 'Search complete.';
  } catch {
    status.textContent = 'The search could not finish. Try again in a moment.';
  } finally {
    submit.disabled = false;
  }
});

for (const example of document.querySelectorAll('.examples button')) {
  example.addEventListener('click', () => {
    if (submit.disabled) return;
    question.value = example.textContent;
    form.requestSubmit();
  });
}

const requestedSource = new URLSearchParams(location.search).get('source');
if (requestedSource) void showSource(requestedSource);
json('/api/health')
  .then(info => { document.querySelector('#index-state').textContent = `${info.documents} documents indexed`; })
  .catch(() => { document.querySelector('#index-state').textContent = 'Index unavailable'; });
json('/api/documents')
  .then(documents => {
    const titles = documents.map(doc => {
      const item = document.createElement('li');
      item.textContent = doc.title;
      return item;
    });
    document.querySelector('#library-list').replaceChildren(...titles);
  })
  .catch(() => { document.querySelector('#library-list').textContent = 'Library unavailable'; });
