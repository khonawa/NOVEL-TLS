const { contextBridge, ipcRenderer, webUtils } = require('electron');
const listeners = [];
contextBridge.exposeInMainWorld('desktop', {
  pick: kind => ipcRenderer.invoke('pick', kind),
  defaultOutput: () => ipcRenderer.invoke('default-output'),
  coverPreview: () => ipcRenderer.invoke('cover-preview'),
  suggestTitle: () => ipcRenderer.invoke('suggest-title'),
  cancelTitle: () => ipcRenderer.invoke('cancel-title'),
  previewText: encoding => ipcRenderer.invoke('text-preview', encoding),
  cancelPreview: () => ipcRenderer.invoke('cancel-text-preview'),
  // File.path was removed in Electron 32; webUtils is the supported replacement.
  dropInput: file => ipcRenderer.invoke('drop-input', webUtils.getPathForFile(file)),
  command: message => ipcRenderer.invoke('command', message),
  openResult: folder => ipcRenderer.invoke('open-result', folder),
  restartBackend: () => ipcRenderer.invoke('restart-backend'),
  onEvent: callback => {
    const handler = (_, event) => callback(event);
    listeners.push(handler);
    ipcRenderer.on('event', handler);
    return () => {
      const index = listeners.indexOf(handler);
      if (index >= 0) {
        listeners.splice(index, 1);
        ipcRenderer.removeListener('event', handler);
      }
    };
  }
});