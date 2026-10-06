const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('desktop', {
  pick: (kind, name) => ipcRenderer.invoke('pick', kind, name),
  command: message => ipcRenderer.invoke('command', message),
  openResult: folder => ipcRenderer.invoke('open-result', folder),
  onEvent: callback => { ipcRenderer.on('event', (_, event) => callback(event)); }
});