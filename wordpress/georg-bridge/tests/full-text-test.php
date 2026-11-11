<?php
// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Command-line tests for includes/sermon.php: `php tests/full-text-test.php`. Exits non-zero
 * on failure.
 *
 * @package GeorgBridge
 */

require __DIR__ . '/../includes/sermon.php';

$georg_failures = 0;
$georg_cases    = array(
	array( null, null, 'absent leaves the meta alone' ),
	array( true, 'set', 'true sets the meta' ),
	array( false, 'delete', 'false deletes the meta' ),
	array( '1', 'set', '"1" sets the meta' ),
	array( 'true', 'set', '"true" sets the meta' ),
	array( '0', 'delete', '"0" deletes the meta' ),
	array( 'false', 'delete', '"false" deletes the meta' ),
	array( 'maybe', null, 'a non-boolean leaves the meta alone' ),
);
foreach ( $georg_cases as $georg_case ) {
	list( $georg_value, $georg_expected, $georg_what ) = $georg_case;
	$georg_ok = georg_bridge_full_text_action( $georg_value ) === $georg_expected;
	echo ( $georg_ok ? 'PASS ' : 'FAIL ' ) . $georg_what . "\n";
	if ( ! $georg_ok ) {
		++$georg_failures;
	}
}
exit( $georg_failures ? 1 : 0 );
