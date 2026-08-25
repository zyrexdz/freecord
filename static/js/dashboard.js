// FreeCord Dashboard Client Helpers

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    const bgClass = type === 'success' ? 'bg-emerald-600' : (type === 'error' ? 'bg-rose-600' : 'bg-indigo-600');
    
    toast.className = `flex items-center gap-3 px-4 py-3 rounded-xl text-white shadow-xl ${bgClass} transform transition-all duration-300 translate-y-4 opacity-0 z-50`;
    toast.innerHTML = `
        <i data-lucide="${type === 'success' ? 'check-circle' : (type === 'error' ? 'alert-octagon' : 'info')}" class="w-5 h-5"></i>
        <span class="text-sm font-medium">${message}</span>
    `;

    container.appendChild(toast);
    if (window.lucide) lucide.createIcons();

    setTimeout(() => {
        toast.classList.remove('translate-y-4', 'opacity-0');
        toast.classList.add('translate-y-0', 'opacity-100');
    }, 10);

    setTimeout(() => {
        toast.classList.remove('translate-y-0', 'opacity-100');
        toast.classList.add('translate-y-4', 'opacity-0');
        setTimeout(() => toast.remove(), 300);
    }, 3500);
}

function copyToClipboard(text, successMsg = 'Copied to clipboard!') {
    navigator.clipboard.writeText(text).then(() => {
        showToast(successMsg, 'success');
    }).catch(err => {
        showToast('Failed to copy: ' + err, 'error');
    });
}

// Auto dismiss URL query parameters after reading
document.addEventListener('DOMContentLoaded', () => {
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has('success')) {
        showToast(urlParams.get('success'), 'success');
    }
    if (urlParams.has('error')) {
        showToast(urlParams.get('error'), 'error');
    }
});
