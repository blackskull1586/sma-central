'use strict';
document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll('[data-copy]').forEach(button => {
  button.addEventListener('click', async () => {
    const field = document.getElementById(button.dataset.copy);
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(field.value);
      if (status) status.textContent = 'Copiado.';
    } catch {
      field.focus(); field.select();
      if (status) status.textContent = 'Texto selecionado. Use a opção Copiar do dispositivo.';
    }
  });
});

document.querySelectorAll('.account-menu').forEach(menu => {
  document.addEventListener('click', event => {
    if (!menu.contains(event.target)) menu.open = false;
  });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });
});

(() => {
  const focusCentralErrors = () => {
    const summary = document.querySelector('.central-admin .error-summary');
    if (summary) {
      summary.tabIndex = -1;
      summary.focus();
    }
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', focusCentralErrors, {once: true});
  } else {
    focusCentralErrors();
  }
})();
