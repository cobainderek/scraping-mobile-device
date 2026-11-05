# scraping-mobile-device

Painel de Inventário de Dispositivos USB

Aplicativo desktop para Ubuntu que lê o número de série (S/N) e outras informações de smartphones Android conectados por USB, mesmo com a tela quebrada ou o aparelho bloqueado. Feito para catalogar ativos de TI.

## Como funciona

Três métodos independentes são disparados ao clicar em **Escanear Dispositivos**:

1. **`lsusb -v`**: lê o descritor USB (`iSerial`, fabricante, produto, modo USB). Funciona sem depuração USB e com o aparelho bloqueado.
2. **ADB**: `adb devices -l`. Se o aparelho estiver autorizado, também lê versão do Android, patch de segurança, bateria etc.
3. **Fastboot**: `fastboot devices` + `getvar` para aparelhos em modo bootloader.

Outros recursos:

- Aviso na tela assim que um celular é plugado (monitor leve via `/sys`).
- Aviso quando o aparelho já existe na coleção (na faixa do topo e no log).
- Coleção persistente em `~/.local/share/inventario_usb/inventario.json`: cada varredura soma, sem apagar o que já foi lido.
- Painel de detalhes por aparelho e exportação para CSV.
- A varredura roda em thread separada, então a janela não trava.

Se `adb` ou `fastboot` não estiverem instalados, o app avisa no log e continua só com o `lsusb`.

## Instalação (Ubuntu)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-tk usbutils android-tools-adb fastboot
python3 -m venv --system-site-packages venv
venv/bin/pip install -r requirements.txt
```

### Permissão USB sem sudo

```bash
sudo cp 51-android-inventario.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev $USER   # depois, logout e login
```

## Uso

```bash
venv/bin/python painel_inventario.py
```

## Limites

- Se o Linux nem enumera o aparelho (erros `device descriptor read/64, error -32` no `dmesg`), nenhum método consegue ler. Costuma ser cabo só de carga, conector danificado ou bateria zerada.
- Com o aparelho bloqueado e sem autorização de ADB, só o `lsusb` entrega dados (S/N, modelo, ID e modo USB).
- Fabricantes reconhecidos ficam em `VENDORS` no código e na regra udev; para incluir outro, adicione o vendor ID nos dois.
