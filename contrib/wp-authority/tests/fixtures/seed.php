<?php
// SPDX-License-Identifier: AGPL-3.0-only
$t = function ( $name, $tax ) {
	$found = term_exists( $name, $tax );
	return (int) ( $found ? $found['term_id'] : wp_insert_term( $name, $tax )['term_id'] );
};
$sermons = array(
	array( 'wpa Rich Toward God', '2025-08-03', 'Luke 12:13-21, Ecclesiastes 1:2, 12-14, 2:18-26', 'All Saints Sunday', 'Vicar Ross Davis' ),
	array( 'wpa Opened', '2025-09-07', 'James 2:1-10, 14-18; Mark 7:24-37', 'All Saints', 'Rev. Ross Davis' ),
	array( 'wpa Children', '2024-10-06', 'Mark 10: 2-16', '3rd Sunday after Pentecost', 'Rev. Ross Davis' ),
);
$ids = array();
foreach ( $sermons as $s ) {
	$id = wp_insert_post( array( 'post_type' => 'ctc_sermon', 'post_status' => 'publish', 'post_title' => $s[0], 'post_date' => $s[1] . ' 10:00:00' ) );
	wp_set_object_terms( $id, array( $t( $s[2], 'ctc_sermon_book' ) ), 'ctc_sermon_book' );
	wp_set_object_terms( $id, array( $t( $s[3], 'ctc_sermon_series' ) ), 'ctc_sermon_series' );
	wp_set_object_terms( $id, array( $t( $s[4], 'ctc_sermon_speaker' ) ), 'ctc_sermon_speaker' );
	$ids[] = $id;
}
$t( 'wpa Unused Speaker', 'ctc_sermon_speaker' );
echo "\n@@WP-AUTHORITY@@" . wp_json_encode( $ids ) . "\n";
