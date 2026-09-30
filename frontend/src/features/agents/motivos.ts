/**
 * Los motivos que el agente deja escritos en su resultado, traducidos.
 *
 * ## Por qué el agente manda un código **y** un texto
 *
 * Porque los dos resuelven cosas distintas y ninguno basta solo. El texto es evidencia: es lo
 * que el agente vio, con sus palabras, y es lo que hay guardado en el trabajo. El código es lo
 * que permite **traducirlo**: sin él, una pantalla en inglés leería el motivo de un escaneo en
 * español, y eso no es un detalle, es el síntoma de haber puesto el idioma en el sitio
 * equivocado.
 *
 * Por eso el código va **junto** al texto y no en lugar de él. Añadir un campo no invalida los
 * resultados ya guardados, y un motivo guardado por un agente anterior —que no mandaba código—
 * se sigue enseñando en su texto original, que es mejor que un hueco.
 *
 * ## Por qué un texto desconocido se enseña en crudo y no se oculta
 *
 * Porque un motivo que no se reconoce es información, no ruido. Un `tar` corrupto, una capa que
 * no se abre: son casos que el agente no tenía previstos y que el operador necesita leer. Escaparlo
 * a la nada sería truthful en apariencia y falso en la realidad —parecería que no hay motivo
 * cuando sí lo hay— y esconderlo detrás de «inventario incompleto» sería mentir en la dirección
 * contraria, que es peor: haría creer que se sabe la causa cuando no se sabe.
 */

import { useTranslation } from 'react-i18next'

/** De código de motivo a clave de traducción. */
const CLAVE_DEL_MOTIVO: Record<string, string> = {
  cve_sin_paquetes_afectados: 'limits.vulnerabilities',
  cve_sin_puertos_afectados: 'limits.vulnerabilities',
  familia_rpm: 'limits.rpm',
  sin_base_de_paquetes: 'limits.sinBaseDePaquetes',
  prefijo_truncado: 'limits.prefijoTruncado',
}

/**
 * Los datos que algunos motivos necesitan para su frase.
 *
 * Es un `Record` y no una interfaz con campos opcionales porque la firma de i18next exige un
 * indice de cadena: con `interface` TypeScript rechaza `t(clave, datos)` aunque los campos
 * coincidan, porque no puede probar que no aparezca un `{{loQueSea}}` que no está en el tipo.
 * Un `Record<string, string | number>` es lo que el diccionario de traducciones **es**.
 */
export type DatosDelMotivo = Record<string, string | number>

export function useMotivo(): (
  codigo: string | null | undefined,
  texto: string | null | undefined,
  datos?: Partial<DatosDelMotivo>
) => string | null {
  const { t } = useTranslation('agents')
  return (codigo, texto, datos) => {
    if (codigo && codigo in CLAVE_DEL_MOTIVO) {
      // Los datos que no llegan se **quitan**, no se pasan como `undefined`. La razón es que
      // `total_en_el_prefijo` solo viene en los resultados recortados: un escaneo de red entero
      // no lo trae, y pasarlo tal cual dejaría un `{{total}}` vacío en medio de la frase. Se
      // filtra aquí y no en quien llama porque el que llama no sabe qué campos espera cada
      // motivo, y ese es justamente el motivo por el que existe este hook.
      const completos: DatosDelMotivo = {}
      for (const [clave, valor] of Object.entries(datos ?? {})) {
        if (valor !== undefined && valor !== null) {
          completos[clave] = valor
        }
      }
      return t(CLAVE_DEL_MOTIVO[codigo], completos)
    }
    if (texto && texto.length > 0) {
      return texto
    }
    return null
  }
}
