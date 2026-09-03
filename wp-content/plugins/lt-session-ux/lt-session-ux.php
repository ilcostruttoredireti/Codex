<?php
/**
 * Plugin Name: LaTestata.it – UX sessione scaduta (bozze)
 * Description: Migliora l'esperienza quando la sessione/nonce WP è scaduta durante la compilazione del form “Pubblica con noi”. Non pubblica mai contenuti: solo bozza.
 * Author: LaTestata.it
 * Version: 0.1.0
 *
 * Sicuro per ambienti non‑prod: questo plugin non altera il database né pubblica articoli.
 */

// Exit if accessed directly.
if (!defined('ABSPATH')) {
    exit;
}

/**
 * Decide se caricare gli asset sulla pagina corrente.
 * Carichiamo solo nelle pagine area-personale, per ridurre l'impatto.
 */
function lt_session_ux_should_enqueue(): bool {
    $uri = isset($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '';
    // Target: /area-personale/* (es. /area-personale/pubblica/)
    if (strpos($uri, '/area-personale/') !== false) {
        return true;
    }
    return false;
}

/**
 * Enqueue script e stile front-end.
 */
function lt_session_ux_enqueue_assets() {
    if (!lt_session_ux_should_enqueue()) {
        return;
    }

    // Assicuriamoci che apiFetch sia disponibile e che l'header X-WP-Nonce venga aggiunto.
    wp_enqueue_script('wp-api-fetch');

    // Plugin assets.
    $plugin_url  = plugin_dir_url(__FILE__);
    $plugin_path = plugin_dir_path(__FILE__);
    $ver = file_exists($plugin_path . 'assets/js/session-ux.js')
        ? (string) filemtime($plugin_path . 'assets/js/session-ux.js')
        : '1';

    wp_enqueue_style(
        'lt-session-ux',
        $plugin_url . 'assets/css/session-ux.css',
        array(),
        $ver
    );

    wp_enqueue_script(
        'lt-session-ux',
        $plugin_url . 'assets/js/session-ux.js',
        array('wp-api-fetch'),
        $ver,
        true
    );

    // Dati per lo script.
    $current_url = (is_ssl() ? 'https://' : 'http://') . $_SERVER['HTTP_HOST'] . $_SERVER['REQUEST_URI'];
    $localized = array(
        'restUrl'        => esc_url_raw(rest_url()),
        // Nonce REST per richieste lato client. Scadrà come da policy WP.
        'restNonce'      => wp_create_nonce('wp_rest'),
        // Login WP con redirect di ritorno alla stessa pagina.
        'reloginUrl'     => wp_login_url($current_url),
        // Copie in italiano, coerenti con l'area personale.
        'copy' => array(
            'title'        => 'Sessione scaduta',
            'message'      => 'La tua sessione è scaduta o non è più autorizzata. Nessun contenuto è andato perso: puoi accedere di nuovo e poi salvare la bozza. Per limiti del browser, l’immagine di copertina potrebbe dover essere selezionata di nuovo.',
            'primary'      => 'Accedi di nuovo',
            'secondary'    => 'Resta qui',
            'savingDraft'  => 'Riprovo a salvare la bozza…',
            'retrySaved'   => 'Bozza salvata. Puoi continuare a scrivere.',
            'retryFailed'  => 'Salvataggio bozza non riuscito. Riprova dopo l’accesso.',
        ),
        // Intervallo ping (secondi) per rilevare anticipo scadenza.
        'pingIntervalSec' => 120,
        // Se riaccedi, tentiamo un click “Salva bozza” al ritorno.
        'autoRetrySelectors' => array(
            // Proviamo i casi più comuni; non fa danni se assenti.
            'button[data-action=\"save-draft\"]',
            'button#save-draft',
            'button[name=\"save-draft\"]',
            'button:contains(\"Salva bozza\")'
        ),
    );

    wp_localize_script('lt-session-ux', 'LT_SESSION_UX', $localized);
}
add_action('wp_enqueue_scripts', 'lt_session_ux_enqueue_assets', 20);

/**
 * Piccolo endpoint di ping (non scrive nulla) per verificare lo stato sessione.
 * Ritorna { ok: true } se autenticati, 401/403 se non validi.
 */
function lt_session_ux_register_routes() {
    register_rest_route('lt/v1', '/ping', array(
        'methods'  => 'GET',
        'permission_callback' => function () {
            // Utente autenticato? Qualsiasi ruolo va bene per il ping.
            return is_user_logged_in();
        },
        'callback' => function (\WP_REST_Request $req) {
            return new \WP_REST_Response(array('ok' => true), 200);
        },
    ));
}
add_action('rest_api_init', 'lt_session_ux_register_routes');

/**
 * All'avvio pagina, se è stato impostato il flag di ritentare la bozza
 * dopo il login, inviamo un piccolo script inline che scatena l'auto-click.
 * Notare: non conosciamo esattamente il bottone; lo script userà i selettori
 * configurati e, se non trova nulla, non fa nulla.
 */
function lt_session_ux_inline_bootstrap() {
    if (!lt_session_ux_should_enqueue()) {
        return;
    }
    $script = <<<JS
    (function(){
      try {
        if (sessionStorage.getItem('lt-retry-draft') === '1') {
          sessionStorage.removeItem('lt-retry-draft');
          // Ritarda per lasciare inizializzare l'interfaccia custom.
          setTimeout(function(){
            if (window.LT_SESSION_UX && Array.isArray(window.LT_SESSION_UX.autoRetrySelectors)) {
              var selectors = window.LT_SESSION_UX.autoRetrySelectors;
              for (var i=0;i<selectors.length;i++){
                var sel = selectors[i];
                // :contains non è CSS standard: gestiamo il caso manualmente.
                if (sel.indexOf(':contains(') !== -1) {
                  var text = sel.split(':contains(')[1];
                  text = text ? text.replace(/\\)$/, '') : '';
                  var candidates = document.querySelectorAll('button, [role=\"button\"], .btn');
                  for (var j=0;j<candidates.length;j++){
                    if ((candidates[j].innerText||'').trim() === text.replace(/\"/g,'')) {
                      candidates[j].click();
                      return;
                    }
                  }
                } else {
                  var el = document.querySelector(sel);
                  if (el) { el.click(); return; }
                }
              }
            }
          }, 1800);
        }
      } catch(e) {}
    })();
    JS;
    wp_add_inline_script('lt-session-ux', $script, 'before');
}
add_action('wp_enqueue_scripts', 'lt_session_ux_inline_bootstrap', 30);

